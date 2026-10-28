from io import BytesIO
from sys import exit
from datetime import datetime
import json
import logging
import os
import re
import tempfile
import shutil
import time

from PIL import Image
from telethon import TelegramClient, errors, sync
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
import telethon.tl.types

from .db import User, Message, Media, Chat, Topic
from . import parse_chat_ref


class Sync:
    """
    Sync iterates and receives messages from the Telegram group to the
    local SQLite DB.
    """
    config = {}
    db = None
    _dialogs = None

    def __init__(self, config, session_file, db):
        self.config = config
        self.db = db
        self._dialogs = None

        self.client = self.new_client(session_file, config)

        if not os.path.exists(self.config["media_dir"]):
            os.mkdir(self.config["media_dir"])

    def sync(self, ids=None, from_id=None, chat_filter=None, dry_run=False):
        """
        Sync syncs messages from Telegram from the last synced message
        into the local SQLite DB for every configured chat.

        If chat_filter (a regex) is passed, only chats whose titles
        match it are synced. If dry_run is True, the chats are resolved
        and reported, but no messages are synced.
        """
        chats = self._select_chats(chat_filter)
        if not chats:
            if chat_filter:
                logging.critical(
                    "no chats matched the filter '{}'".format(chat_filter))
            else:
                logging.critical(
                    "no chats configured. set 'group' or 'chats' in the config")
            exit(1)

        # Resolve all inputs (IDs, URLs, usernames, display names or
        # entities) to entity IDs before syncing.
        resolved = [(self._resolve_chat_id(c), self._chat_label(c))
                    for c in chats]

        if dry_run:
            logging.info(
                "[dry-run] {} chat(s) would be synced:".format(len(resolved)))
            for chat_id, label in resolved:
                logging.info(
                    "[dry-run] - {} (id={})".format(label, chat_id))
            if self.config.get("use_takeout", False):
                self.finish_takeout()
            return 0

        total = 0
        if ids or from_id:
            for chat_id, _ in resolved:
                total += self._sync_chat(chat_id, ids, from_id)
        else:
            total = self._sync_newest(resolved)

        if self.config.get("fetch_user_bios", False):
            self._fetch_user_bios()

        self.db.commit()
        if self.config.get("use_takeout", False):
            self.finish_takeout()
        logging.info(
            "finished. fetched {} messages across {} chat(s)".format(total, len(resolved)))

    def _sync_newest(self, resolved):
        """Сначала свежие сообщения всех чатов, затем история от новых к старым."""
        counts = {chat_id: 0 for chat_id, _ in resolved}
        failed = set()
        entities = {}
        limit = self.config['fetch_limit']

        def allowed(chat_id):
            return chat_id not in failed and (not limit or counts[chat_id] < limit)

        def fetch(chat_id, floor_id, offset_id, size):
            if limit:
                size = min(size, limit - counts[chat_id])
            try:
                count = self._sync_newest_page(chat_id, floor_id, offset_id, size)
                counts[chat_id] += count
            except Exception:
                self.db.conn.rollback()
                failed.add(chat_id)
                logging.exception('[%s] ошибка импорта; остальные чаты продолжат загрузку', chat_id)

        def refresh():
            for chat_id, _ in resolved:
                if not allowed(chat_id):
                    continue
                try:
                    if chat_id not in entities:
                        entity = self._get_entity(chat_id)
                        self._upsert_chat(entity)
                        self.db.commit()
                        entities[chat_id] = entity
                    floor_id, _ = self.db.get_last_message_id(chat_id=chat_id)
                    fetch(chat_id, floor_id, 0,
                          min(100, self.config['fetch_batch_size']))
                except Exception:
                    self.db.conn.rollback()
                    failed.add(chat_id)
                    logging.exception('[%s] ошибка обновления чата', chat_id)

        refresh()
        refreshed_at = time.monotonic()
        while True:
            pending = [(chat_id, gaps[0]) for chat_id, _ in resolved
                       if allowed(chat_id)
                       and (gaps := self.db.get_sync_gaps(chat_id))]
            if not pending:
                break
            for chat_id, (floor_id, offset_id) in pending:
                fetch(chat_id, floor_id, offset_id, self.config['fetch_batch_size'])
                if time.monotonic() - refreshed_at >= 300:
                    refresh()
                    refreshed_at = time.monotonic()
            if any(allowed(chat_id) and self.db.get_sync_gaps(chat_id)
                   for chat_id, _ in resolved):
                time.sleep(self.config['fetch_wait'])

        for chat_id, entity in entities.items():
            if chat_id not in failed:
                self._backfill_topics(entity, chat_id)
                logging.info('[%s] загружено %s сообщений; осталось промежутков: %s',
                             chat_id, counts[chat_id], len(self.db.get_sync_gaps(chat_id)))
        if failed:
            raise RuntimeError('Не удалось загрузить чаты: {}'.format(sorted(failed)))
        return sum(counts.values())

    def _sync_newest_page(self, chat_id, floor_id, offset_id, size):
        """Сообщения и позиция истории фиксируются одной транзакцией."""
        count = 0
        oldest_id = None
        for m in self._get_messages(chat_id, offset_id, newest_first=True,
                                    min_id=floor_id, limit=size):
            if m is None:
                continue
            self.db.insert_user(m.user)
            if m.media:
                self.db.insert_media(m.media)
            self.db.insert_message(m)
            oldest_id = m.id
            count += 1
        if oldest_id is not None:
            self.db.save_sync_gap(chat_id, floor_id, oldest_id)
        elif offset_id:
            self.db.delete_sync_gap(chat_id, floor_id)
        self.db.commit()
        logging.info('[%s] от новых к старым: загружено %s, нижняя граница %s, позиция %s',
                     chat_id, count, floor_id, oldest_id or offset_id)
        return count

    def _resolve_chat_id(self, chat):
        """
        Resolves a chat reference (chat ID, t.me URL, @username, display
        name) or an already resolved entity into an entity ID.
        """
        if hasattr(chat, "id"):
            return chat.id
        return self._get_group_id(chat)

    def _chat_label(self, chat):
        """Returns a human readable label for a chat reference or entity."""
        if hasattr(chat, "id"):
            if getattr(chat, "title", None):
                return chat.title
            if getattr(chat, "username", None):
                return "@{}".format(chat.username)
            return str(chat.id)
        return str(chat)

    def _get_chats(self):
        """
        Returns the list of chats to sync. Supports the `chats` list config
        and falls back to the legacy single `group` config for backward
        compatibility.
        """
        chats = self.config.get("chats") or []
        if isinstance(chats, (str, int)):
            chats = [chats]
        if not chats and self.config.get("group"):
            chats = [self.config["group"]]
        return chats

    def _select_chats(self, chat_filter=None):
        """
        Returns the list of chats to sync: the configured / CLI chats if
        any, otherwise all of the user's group / channel dialogs.

        When chat_filter (a regex) is passed, only chats whose titles
        match the filter are returned. With a filter, chats are matched
        against the user's dialogs, so every returned chat is a resolved
        entity.
        """
        chats = self._get_chats()
        if chats and not chat_filter:
            # No filter: keep the legacy behavior of resolving each
            # reference during the sync loop.
            return chats

        dialogs = self._get_dialogs()

        if chats:
            selected = []
            for ref in chats:
                d = self._match_dialog(ref)
                if d is not None:
                    selected.append(d)
                else:
                    logging.warning(
                        "chat '{}' not found in your dialogs. skipping".format(ref))
        else:
            # No explicit chats: match the filter against all dialogs.
            selected = [d for d in dialogs if d.is_group or d.is_channel]

        if chat_filter:
            rx = re.compile(chat_filter)
            selected = [d for d in selected if d.title and rx.search(d.title)]

        return [d.entity for d in selected]

    def list_chats(self):
        """
        Lists all the dialogs available to the authorized user with
        their IDs, usernames, and titles.
        """
        dialogs = self._get_dialogs()
        logging.info("{} dialog(s) available:".format(len(dialogs)))
        for d in dialogs:
            username = getattr(d.entity, "username", None)
            logging.info("id={} username={} title={}".format(
                d.id, "@{}".format(username) if username else "-", d.title))
        if self.config.get("use_takeout", False):
            self.finish_takeout()

    def _sync_chat(self, chat, ids=None, from_id=None) -> int:
        """
        Sync a single chat into the DB. Returns the number of messages fetched.
        """
        entity = self._get_entity(chat)
        group_id = entity.id

        # Refresh the chat's metadata (title, description, forum flag...)
        # on every run.
        self._upsert_chat(entity)

        if ids:
            last_id, last_date = (ids, None)
            logging.info("[{}] fetching message id={}".format(chat, ids))
        elif from_id:
            last_id, last_date = (from_id, None)
            logging.info(
                "[{}] fetching from last message id={}".format(chat, last_id))
        else:
            last_id, last_date = self.db.get_last_message_id(chat_id=group_id)
            logging.info("[{}] fetching from last message id={} ({})".format(
                chat, last_id, last_date))

        n = 0
        while True:
            has = False
            for m in self._get_messages(group_id,
                                        offset_id=last_id if last_id else 0,
                                        ids=ids):
                if not m:
                    continue

                has = True

                # Insert the records into DB.
                self.db.insert_user(m.user)

                if m.media:
                    self.db.insert_media(m.media)

                self.db.insert_message(m)

                last_date = m.date
                n += 1
                if n % 300 == 0:
                    logging.info("fetched {} messages".format(n))
                    self.db.commit()

                if 0 < self.config["fetch_limit"] <= n or ids:
                    has = False
                    break

            self.db.commit()
            if has:
                last_id = m.id
                logging.info("[{}] fetched {} messages. sleeping for {} seconds".format(
                    chat, n, self.config["fetch_wait"]))
                time.sleep(self.config["fetch_wait"])
            else:
                break

        self._backfill_topics(entity, group_id)

        logging.info("[{}] done. fetched {} messages. last message = {}".format(
            chat, n, last_date))
        return n

    def new_client(self, session, config):
        if "proxy" in config and config["proxy"].get("enable"):
            proxy = config["proxy"]
            client = TelegramClient(session, config["api_id"], config["api_hash"], proxy=(proxy["protocol"], proxy["addr"], proxy["port"]))
        else:
            client = TelegramClient(session, config["api_id"], config["api_hash"])
        # hide log messages
        # upstream issue https://github.com/LonamiWebs/Telethon/issues/3840
        client_logger = client._log["telethon.client.downloads"]
        client_logger._info = client_logger.info

        def patched_info(*args, **kwargs):
            if (
                args[0] == "File lives in another DC" or
                args[0] == "Starting direct file download in chunks of %d at %d, stride %d"
            ):
                return client_logger.debug(*args, **kwargs)
            client_logger._info(*args, **kwargs)
        client_logger.info = patched_info

        client.start()
        if config.get("use_takeout", False):
            for retry in range(3):
                try:
                    takeout_client = client.takeout(finalize=True).__enter__()
                    # check if the takeout session gets invalidated
                    takeout_client.get_messages("me")
                    return takeout_client
                except errors.TakeoutInitDelayError as e:
                    logging.info(
                        "please allow the data export request received from Telegram on your device. "
                        "you can also wait for {} seconds.".format(e.seconds))
                    logging.info(
                        "press Enter key after allowing the data export request to continue..")
                    input()
                    logging.info("trying again.. ({})".format(retry + 2))
                except errors.TakeoutInvalidError:
                    logging.info("takeout invalidated. delete the session.session file and try again.")
            else:
                logging.info("could not initiate takeout.")
                raise(Exception("could not initiate takeout."))
        else:
            return client

    def finish_takeout(self):
        self.client.__exit__(None, None, None)

    def _get_messages(self, group, offset_id, ids=None, *, newest_first=False,
                      min_id=0, limit=None) -> Message:
        if newest_first:
            messages = self._fetch_messages(group, offset_id, ids,
                                            newest_first=True, min_id=min_id, limit=limit)
        else:
            messages = self._fetch_messages(group, offset_id, ids)
        # https://docs.telethon.dev/en/latest/quick-references/objects-reference.html#message
        for m in messages:
            if not m:
                continue

            # Media.
            sticker = None
            med = None
            if m.media:
                # If it's a sticker, get the alt value (unicode emoji).
                if isinstance(m.media, telethon.tl.types.MessageMediaDocument) and \
                        hasattr(m.media, "document") and \
                        m.media.document.mime_type == "application/x-tgsticker":
                    alt = [a.alt for a in m.media.document.attributes if isinstance(
                        a, telethon.tl.types.DocumentAttributeSticker)]
                    if len(alt) > 0:
                        sticker = alt[0]
                elif isinstance(m.media, telethon.tl.types.MessageMediaPoll):
                    med = self._make_poll(m)
                else:
                    med = self._get_media(m)

            # Message.
            typ = "message"
            if m.action:
                if isinstance(m.action, telethon.tl.types.MessageActionChatAddUser):
                    typ = "user_joined"
                elif isinstance(m.action, telethon.tl.types.MessageActionChatJoinedByLink):
                    typ = "user_joined_by_link"
                elif isinstance(m.action, telethon.tl.types.MessageActionChatDeleteUser):
                    typ = "user_left"

            # Forum topic metadata from service messages.
            if m.action:
                if isinstance(m.action, telethon.tl.types.MessageActionTopicCreate):
                    self.db.record_topic_action(
                        chat_id=group,
                        thread_id=m.id,
                        message_id=m.id,
                        title=m.action.title,
                        creator_id=getattr(m, "sender_id", None),
                        date=m.date,
                        closed=False)
                elif isinstance(m.action, telethon.tl.types.MessageActionTopicEdit):
                    thread_id = self._get_thread_id(m)
                    if thread_id:
                        self.db.record_topic_action(group, thread_id, m.id,
                                             title=getattr(m.action, "title", None),
                                             closed=getattr(m.action, "closed", None))

            yield Message(
                type=typ,
                id=m.id,
                date=m.date,
                edit_date=m.edit_date,
                content=sticker if sticker else m.raw_text,
                reply_to=getattr(m.reply_to, "reply_to_msg_id", None),
                user=self._get_user(m.sender, m.chat),
                media=med,
                chat_id=group,
                thread_id=self._get_thread_id(m)
            )

    def _get_thread_id(self, m):
        """
        Extract the forum topic (thread) ID that the message belongs to.

        Telethon's Message object has no thread_id attribute, so the value
        is derived from the reply header. In forum groups (topics enabled),
        every message inside a topic carries a reply header with
        forum_topic=True, where reply_to_top_id -- or reply_to_msg_id for
        direct replies to the topic root -- is the ID of the topic's root
        message. The root message itself carries a MessageActionTopicCreate
        action and its own ID is the topic ID.

        Returns None for groups without topics and for messages that are
        not part of any topic.
        """
        # The message that creates the topic is the thread root itself.
        if m.action and isinstance(m.action, telethon.tl.types.MessageActionTopicCreate):
            return m.id

        reply_to = m.reply_to
        if reply_to and getattr(reply_to, "forum_topic", False):
            return getattr(reply_to, "reply_to_top_id", None) or reply_to.reply_to_msg_id

        return None

    def _upsert_chat(self, entity):
        """Store the chat's metadata in the DB, refreshing it on every run."""
        typ = "chat"
        if isinstance(entity, telethon.tl.types.Channel):
            typ = "channel" if entity.broadcast else "supergroup"

        self.db.upsert_chat(Chat(
            id=entity.id,
            title=getattr(entity, "title", None),
            username=getattr(entity, "username", None),
            description=self._get_chat_description(entity),
            type=typ,
            forum=getattr(entity, "forum", False) or False,
            participants_count=getattr(entity, "participants_count", None),
            avatar=None,
            synced_at=datetime.utcnow()))

    def _get_chat_description(self, entity):
        """Fetch the chat's description via the full chat / channel request.
        One extra API call per chat per sync run."""
        try:
            if isinstance(entity, telethon.tl.types.Channel):
                full = self.client(GetFullChannelRequest(entity))
            elif isinstance(entity, telethon.tl.types.Chat):
                full = self.client(GetFullChatRequest(entity.id))
            else:
                return None
            return getattr(full.full_chat, "about", None)
        except Exception as e:
            logging.error(
                "error fetching chat description for {}: {}".format(entity.id, e))
            return None

    def _backfill_topics(self, entity, chat_id):
        """
        Fetch the metadata of topics that were created before topic
        tracking existed: thread IDs present in the chat's messages with
        no row in the topics table.
        """
        missing = self.db.get_missing_topic_ids(chat_id)
        if not missing:
            return

        logging.info("[{}] backfilling {} topic(s)".format(chat_id, len(missing)))
        for m in self._fetch_messages_by_ids(entity, missing):
            if not m or not m.action or \
                    not isinstance(m.action, telethon.tl.types.MessageActionTopicCreate):
                continue
            self.db.record_topic_action(
                chat_id=chat_id,
                thread_id=m.id,
                message_id=m.id,
                title=m.action.title,
                creator_id=getattr(m, "sender_id", None),
                date=m.date,
                closed=False)
        self.db.commit()

    def _fetch_messages_by_ids(self, entity, ids):
        try:
            return self.client.get_messages(entity, ids=ids) or []
        except errors.FloodWaitError as e:
            logging.info(
                "flood waited: have to wait {} seconds".format(e.seconds))
            return []

    def _fetch_user_bios(self):
        """
        Fetch profile bios for the users that don't have one yet. One API
        call per user. Fetched values are cached in the DB; users with no
        bio on Telegram get an empty string so they are never re-fetched.
        """
        user_ids = self.db.get_users_without_bio()
        if not user_ids:
            return

        logging.info("fetching bios for {} user(s)".format(len(user_ids)))
        for uid in user_ids:
            self.db.update_user_bio(uid, self._get_user_bio(uid) or "")
        self.db.commit()

    def _get_user_bio(self, user_id):
        try:
            full = self.client(GetFullUserRequest(user_id))
            return getattr(full.full_user, "about", None)
        except Exception as e:
            logging.error(
                "error fetching bio for user {}: {}".format(user_id, e))
            return None

    def _fetch_messages(self, group, offset_id, ids=None, *, newest_first=False,
                        min_id=0, limit=None) -> Message:
        try:
            if self.config.get("use_takeout", False):
                wait_time = 0
            else:
                wait_time = None
            messages = self.client.get_messages(group, offset_id=offset_id,
                                                limit=limit or self.config["fetch_batch_size"],
                                                wait_time=wait_time,
                                                ids=ids,
                                                min_id=min_id,
                                                reverse=not newest_first)
            return messages
        except errors.FloodWaitError as e:
            logging.info(
                "flood waited: have to wait {} seconds".format(e.seconds))
            raise

    def _get_user(self, u, chat) -> User:
        tags = []

        # if user info is empty, check for message from group
        if (
            u is None and
            chat is not None and
            chat.title != ''
            ):
                tags.append("group_self")
                avatar = self._downloadAvatarForUserOrChat(chat)
                return User(
                    id=chat.id,
                    username=chat.title,
                    first_name=None,
                    last_name=None,
                    tags=tags,
                    avatar=avatar
                )

        is_normal_user = isinstance(u, telethon.tl.types.User)

        if isinstance(u, telethon.tl.types.ChannelForbidden):
            return User(
                id=u.id,
                username=u.title,
                first_name=None,
                last_name=None,
                tags=tags,
                avatar=None
            )

        if is_normal_user:
            if u.bot:
                tags.append("bot")

        if u.scam:
            tags.append("scam")

        if u.fake:
            tags.append("fake")

        # Download sender's profile photo if it's not already cached.
        avatar = self._downloadAvatarForUserOrChat(u)

        return User(
            id=u.id,
            username=u.username if u.username else str(u.id),
            first_name=u.first_name if is_normal_user else None,
            last_name=u.last_name if is_normal_user else None,
            tags=tags,
            avatar=avatar
        )

    def _make_poll(self, msg):
        if not msg.media.results or not msg.media.results.results:
            return None

        options = [{"label": a.text.text, "count": 0, "correct": False}
                   for a in msg.media.poll.answers]

        total = msg.media.results.total_voters
        if msg.media.results.results:
            for i, r in enumerate(msg.media.results.results):
                options[i]["count"] = r.voters
                options[i]["percent"] = r.voters / \
                    total * 100 if total > 0 else 0
                options[i]["correct"] = r.correct

        return Media(
            id=msg.id,
            type="poll",
            url=None,
            title=msg.media.poll.question.text,
            description=json.dumps(options),
            thumb=None
        )

    def _get_media(self, msg):
        if isinstance(msg.media, telethon.tl.types.MessageMediaWebPage) and \
                isinstance(msg.media.webpage, telethon.tl.types.WebPage):
            return Media(
                id=msg.id,
                type="webpage",
                url=msg.media.webpage.url,
                title=msg.media.webpage.title,
                description=msg.media.webpage.description if msg.media.webpage.description else None,
                thumb=None
            )
        elif isinstance(msg.media, telethon.tl.types.MessageMediaPhoto) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaDocument) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaContact):
            if self.config["download_media"]:
                # Filter by extensions?
                if len(self.config["media_mime_types"]) > 0:
                    if hasattr(msg, "file") and hasattr(msg.file, "mime_type") and msg.file.mime_type:
                        if msg.file.mime_type not in self.config["media_mime_types"]:
                            logging.info(
                                "skipping media #{} / {}".format(msg.file.name, msg.file.mime_type))
                            return

                logging.info("downloading media #{}".format(msg.id))
                try:
                    basename, fname, thumb = self._download_media(msg)
                    return Media(
                        id=msg.id,
                        type="photo",
                        url=fname,
                        title=basename,
                        description=None,
                        thumb=thumb
                    )
                except Exception as e:
                    logging.error(
                        "error downloading media: #{}: {}".format(msg.id, e))

    def _download_media(self, msg) -> [str, str, str]:
        """
        Download a media / file attached to a message and return its original
        filename, sanitized name on disk, and the thumbnail (if any). 
        """
        # Download the media to the temp dir and copy it back as
        # there does not seem to be a way to get the canonical
        # filename before the download.
        fpath = self.client.download_media(msg, file=tempfile.gettempdir())
        basename = os.path.basename(fpath)

        newname = "{}.{}".format(msg.id, self._get_file_ext(basename))
        shutil.move(fpath, os.path.join(self.config["media_dir"], newname))

        # If it's a photo, download the thumbnail.
        tname = None
        if isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
            tpath = self.client.download_media(
                msg, file=tempfile.gettempdir(), thumb=1)
            tname = "thumb_{}.{}".format(
                msg.id, self._get_file_ext(os.path.basename(tpath)))
            shutil.move(tpath, os.path.join(self.config["media_dir"], tname))

        return basename, newname, tname

    def _get_file_ext(self, f) -> str:
        if "." in f:
            e = f.split(".")[-1]
            if len(e) < 6:
                return e

        return ".file"

    def _download_avatar(self, user):
        fname = "avatar_{}.jpg".format(user.id)
        fpath = os.path.join(self.config["media_dir"], fname)

        if os.path.exists(fpath):
            return fname

        logging.info("downloading avatar #{}".format(user.id))

        # Download the file into a container, resize it, and then write to disk.
        b = BytesIO()
        profile_photo = self.client.download_profile_photo(user, file=b)
        if profile_photo is None:
            logging.info("user has no avatar #{}".format(user.id))
            return None

        im = Image.open(b)
        im.thumbnail(self.config["avatar_size"], Image.LANCZOS)
        im.save(fpath, "JPEG")

        return fname

    def _get_dialogs(self):
        """
        Fetches and caches all dialogs of the authorized user. Fetching
        the dialogs also syncs the entity cache to get latest entities.
        ref: https://docs.telethon.dev/en/latest/concepts/entities.html#getting-entities
        """
        if self._dialogs is None:
            self._dialogs = self.client.get_dialogs()
        return self._dialogs

    def _match_dialog(self, ref):
        """
        Match a chat reference (chat ID, @username, t.me URL, or display
        name) against the authorized user's dialogs. Returns the matching
        dialog, or None if nothing matches.
        """
        try:
            ref = parse_chat_ref(ref)
        except ValueError:
            return None

        if isinstance(ref, int):
            for d in self._get_dialogs():
                # Accept plain entity IDs as well as the marked
                # -<chat_id> / -100<channel_id> peer ID forms.
                if ref in (d.entity.id, d.id, int("-100{}".format(d.entity.id))):
                    return d
            return None

        if ref.startswith("@"):
            username = ref[1:].lower()
            for d in self._get_dialogs():
                if getattr(d.entity, "username", None) and \
                        d.entity.username.lower() == username:
                    return d
            return None

        # Anything else: try an exact, case-insensitive display name match.
        title = ref.lower()
        for d in self._get_dialogs():
            if d.title and d.title.lower() == title:
                return d
        return None

    def _get_group_id(self, group):
        """
        Syncs the Entity cache and returns the Entity ID for the specified group,
        which can be a chat ID, a t.me URL, an @username, or a display name.

        The authorized user must be a part of the group.
        """
        return self._get_entity(group).id

    def _get_entity(self, chat):
        """
        Resolves a chat reference (an entity, a chat ID, a t.me URL, an
        @username, or a display name) into a Telethon entity.

        The authorized user must be a part of the chat.
        """
        if isinstance(chat, (telethon.tl.types.Channel, telethon.tl.types.Chat)):
            return chat

        # Try to match the reference against the user's dialogs first.
        # This also syncs the entity cache to get latest entities.
        match = self._match_dialog(chat)
        if match is not None:
            return match.entity

        # No dialog match: fall back to the entity cache lookup for
        # plain IDs and usernames.
        try:
            # If the passed chat is a chat ID, extract it.
            chat = int(chat)
        except (ValueError, TypeError):
            pass

        try:
            return self.client.get_entity(chat)
        except ValueError:
            logging.critical("the chat: {} does not exist,"
                             " or the authorized user is not a participant!".format(chat))
            # This is a critical error, so exit with code: 1
            exit(1)

    def _downloadAvatarForUserOrChat(self, entity):
        avatar = None
        if self.config["download_avatars"]:
            try:
                fname = self._download_avatar(entity)
                avatar = fname
            except Exception as e:
                logging.error(
                    "error downloading avatar: #{}: {}".format(entity.id, e))
        return avatar
