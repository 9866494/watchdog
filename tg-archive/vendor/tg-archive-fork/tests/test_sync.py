import datetime
import os

import pytz

from tgarchive.db import DB, Message, User
from tgarchive.sync import Sync

DATE = pytz.utc.localize(datetime.datetime(2024, 1, 15, 12, 0, 0))


class FakeUser:
    def __init__(self, id, username):
        self.id = id
        self.username = username
        self.first_name = None
        self.last_name = None
        self.scam = False
        self.fake = False


class FakeMessage:
    """Bare minimum surface of a Telethon message consumed by _get_messages."""

    def __init__(self, id, sender, text="hello", date=DATE):
        self.id = id
        self.date = date
        self.edit_date = None
        self.media = None
        self.action = None
        self.raw_text = text
        self.reply_to = None
        self.sender = sender
        self.sender_id = sender.id if sender is not None else None
        self.chat = None


class FakeEntity:
    """Bare minimum surface of a Telethon chat / channel entity."""

    def __init__(self, id, title="fake chat", username=None,
                 participants_count=None, forum=False):
        self.id = id
        self.title = title
        self.username = username
        self.participants_count = participants_count
        self.forum = forum


class FakeSync(Sync):
    """Sync with the Telegram transport stubbed out (no client, no network)."""

    def __init__(self, config, db, feed):
        self.config = config
        self.db = db
        self.client = None
        # feed: {chat_id: [FakeMessage, ...]} served once per chat.
        self.feed = {g: list(msgs) for g, msgs in feed.items()}
        self.fetch_calls = []
        # Messages served to the topic backfill pass, per chat ID.
        self.backfill_feed = {}
        # Bios served by _get_user_bio, per user ID.
        self.bios = {}
        self.bio_calls = []

    def _get_entity(self, chat):
        if hasattr(chat, "id") and hasattr(chat, "title"):
            return chat
        return FakeEntity(id=int(chat))

    def _get_chat_description(self, entity):
        return None

    def _fetch_messages_by_ids(self, entity, ids):
        return [m for m in self.backfill_feed.get(entity.id, [])
                if m.id in ids]

    def _get_user_bio(self, user_id):
        self.bio_calls.append(user_id)
        return self.bios.get(user_id)

    def _fetch_messages(self, group, offset_id, ids=None):
        self.fetch_calls.append((group, offset_id, ids))
        batch = self.feed.get(group, [])
        self.feed[group] = []
        return batch


def make_config(tmp_path, **over):
    cfg = {
        "api_id": 1,
        "api_hash": "hash",
        "group": "",
        "chats": [],
        "media_dir": os.path.join(str(tmp_path), "media"),
        "download_avatars": False,
        "download_media": False,
        "media_mime_types": [],
        "fetch_batch_size": 100,
        "fetch_wait": 0,
        "fetch_limit": 0,
    }
    cfg.update(over)
    return cfg


def make_message_row(id, chat_id):
    return Message(id=id,
                   type="message",
                   date=DATE,
                   edit_date=None,
                   content="seed",
                   reply_to=None,
                   user=User(id=1, username="alice", first_name=None,
                             last_name=None, tags=[], avatar=None),
                   media=None,
                   chat_id=chat_id)


def test_sync_iterates_over_multiple_chats(tmp_path):
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    alice = FakeUser(1, "alice")
    feed = {
        -1001: [FakeMessage(1, alice, "a1"), FakeMessage(2, alice, "a2")],
        -1002: [FakeMessage(1, alice, "b1"), FakeMessage(2, alice, "b2"),
                FakeMessage(3, alice, "b3")],
    }

    s = FakeSync(make_config(tmp_path, chats=[-1001, -1002]), db, feed)
    s.sync()

    rows = db.conn.execute(
        "SELECT chat_id, id, content FROM messages ORDER BY chat_id, id").fetchall()
    assert rows == [
        (-1002, 1, "b1"), (-1002, 2, "b2"), (-1002, 3, "b3"),
        (-1001, 1, "a1"), (-1001, 2, "a2"),
    ]
    assert {c[0] for c in s.fetch_calls} == {-1001, -1002}


def test_sync_backward_compatible_single_group(tmp_path):
    """Legacy single 'group' config must keep working."""
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    alice = FakeUser(1, "alice")
    feed = {-1001: [FakeMessage(1, alice, "only")]}

    s = FakeSync(make_config(tmp_path, group=-1001), db, feed)
    s.sync()

    assert db.get_last_message_id(chat_id=-1001)[0] == 1
    row = db.conn.execute(
        "SELECT chat_id, content FROM messages").fetchone()
    assert row == (-1001, "only")


def test_sync_resumes_from_per_chat_last_id(tmp_path):
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    # Chat -1001 was previously synced up to message 5; -1002 is new.
    db.insert_message(make_message_row(5, -1001))
    db.commit()

    s = FakeSync(make_config(tmp_path, chats=[-1001, -1002]), db,
                 {-1001: [], -1002: []})
    s.sync()

    first_offsets = {}
    for group, offset, _ in s.fetch_calls:
        first_offsets.setdefault(group, offset)
    assert first_offsets == {-1001: 5, -1002: 0}


def test_get_chats_prefers_chats_over_group():
    s = Sync.__new__(Sync)
    s.config = {"chats": [-1001, -1002], "group": "legacy-group"}
    assert s._get_chats() == [-1001, -1002]


def test_get_chats_falls_back_to_group():
    s = Sync.__new__(Sync)
    s.config = {"chats": [], "group": "legacy-group"}
    assert s._get_chats() == ["legacy-group"]


def test_get_chats_scalar_normalized_to_list():
    s = Sync.__new__(Sync)
    s.config = {"chats": -1001}
    assert s._get_chats() == [-1001]


def test_get_chats_empty_when_unconfigured():
    s = Sync.__new__(Sync)
    s.config = {"chats": [], "group": ""}
    assert s._get_chats() == []
