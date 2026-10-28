import json
import math
import os
import sqlite3
from collections import namedtuple
from datetime import datetime
import pytz
from typing import Iterator

schema = """
CREATE table messages (
    id INTEGER NOT NULL,
    chat_id INTEGER,
    thread_id INTEGER,
    type TEXT NOT NULL,
    date TIMESTAMP NOT NULL,
    edit_date TIMESTAMP,
    content TEXT,
    reply_to INTEGER,
    user_id INTEGER,
    media_id INTEGER,
    PRIMARY KEY (id, chat_id),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(media_id) REFERENCES media(id)
);
##
CREATE table users (
    id INTEGER NOT NULL PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    tags TEXT,
    avatar TEXT,
    bio TEXT
);
##
CREATE table media (
    id INTEGER NOT NULL PRIMARY KEY,
    type TEXT,
    url TEXT,
    title TEXT,
    description TEXT,
    thumb TEXT
);
##
CREATE table chats (
    id INTEGER NOT NULL PRIMARY KEY,
    title TEXT,
    username TEXT,
    description TEXT,
    type TEXT,
    forum INTEGER DEFAULT 0,
    participants_count INTEGER,
    avatar TEXT,
    synced_at TIMESTAMP
);
##
CREATE table topics (
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    title TEXT,
    creator_id INTEGER,
    date TIMESTAMP,
    closed INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, thread_id),
    FOREIGN KEY(chat_id) REFERENCES chats(id)
);
"""

# FTS5 full-text index over message content for keyword searches,
# scoping-friendly via the UNINDEXED chat_id and thread_id columns.
# Kept in sync with the messages table via triggers; INSERT OR REPLACE
# fires the delete + insert triggers, so re-synced messages stay indexed.
fts_schema = """
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    chat_id UNINDEXED,
    thread_id UNINDEXED
);
##
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, chat_id, thread_id)
    VALUES (new.rowid, new.content, new.chat_id, new.thread_id);
END;
##
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id, thread_id)
    VALUES ('delete', old.rowid, old.content, old.chat_id, old.thread_id);
END;
##
CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id, thread_id)
    VALUES ('delete', old.rowid, old.content, old.chat_id, old.thread_id);
    INSERT INTO messages_fts(rowid, content, chat_id, thread_id)
    VALUES (new.rowid, new.content, new.chat_id, new.thread_id);
END;
"""

User = namedtuple(
    "User", ["id", "username", "first_name", "last_name", "tags", "avatar"])

Chat = namedtuple(
    "Chat", ["id", "title", "username", "description", "type", "forum",
             "participants_count", "avatar", "synced_at"])

Topic = namedtuple(
    "Topic", ["chat_id", "thread_id", "title", "creator_id", "date", "closed"],
    defaults=(0,))

Message = namedtuple(
    "Message", ["id", "type", "date", "edit_date", "content", "reply_to", "user", "media", "chat_id", "thread_id"],
    defaults=(None, None))

Media = namedtuple(
    "Media", ["id", "type", "url", "title", "description", "thumb"])

Month = namedtuple("Month", ["date", "slug", "label", "count"])

Day = namedtuple("Day", ["date", "slug", "label", "count", "page"])


def _page(n, multiple):
    return math.ceil(n / multiple)


class DB:
    conn = None
    tz = None

    def __init__(self, dbfile, tz=None):
        # Initialize the SQLite DB. If it's new, create the table schema.
        is_new = not os.path.isfile(dbfile)

        self.conn = sqlite3.Connection(
            dbfile, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)

        # Add the custom PAGE() function to get the page number of a row
        # by its row number and a limit multiple.
        self.conn.create_function("PAGE", 2, _page)

        if tz:
            self.tz = pytz.timezone(tz)

        if is_new:
            for s in schema.split("##"):
                self.conn.cursor().execute(s)
                self.conn.commit()
            self._ensure_fts_index()
        else:
            self._migrate()

        self.conn.execute("""CREATE TABLE IF NOT EXISTS sync_gaps (
            chat_id INTEGER NOT NULL,
            floor_id INTEGER NOT NULL,
            offset_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, floor_id)
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS topic_field_versions (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, thread_id, field)
        )""")
        self.conn.commit()

    def record_topic_action(self, chat_id, thread_id, message_id, title=None,
                            closed=None, creator_id=None, date=None):
        """Старые события не затирают более свежие значения полей топика."""
        self.conn.execute(
            'INSERT OR IGNORE INTO topics (chat_id, thread_id, closed) VALUES (?, ?, 0)',
            (chat_id, thread_id))
        if date is not None:
            self.conn.execute(
                'UPDATE topics SET creator_id=?, date=? WHERE chat_id=? AND thread_id=?',
                (creator_id, date.strftime('%Y-%m-%d %H:%M:%S'), chat_id, thread_id))
        for field, value in [('title', title), ('closed', closed)]:
            if value is None:
                continue
            version = self.conn.execute(
                'SELECT message_id FROM topic_field_versions '
                'WHERE chat_id=? AND thread_id=? AND field=?',
                (chat_id, thread_id, field)).fetchone()
            if version and version[0] >= message_id:
                continue
            self.conn.execute(
                'UPDATE topics SET {}=? WHERE chat_id=? AND thread_id=?'.format(field),
                (int(bool(value)) if field == 'closed' else value, chat_id, thread_id))
            self.conn.execute('INSERT OR REPLACE INTO topic_field_versions VALUES (?, ?, ?, ?)',
                              (chat_id, thread_id, field, message_id))

    def get_sync_gaps(self, chat_id):
        """Незагруженные промежутки истории, начиная с самого свежего."""
        return self.conn.execute(
            'SELECT floor_id, offset_id FROM sync_gaps '
            'WHERE chat_id = ? ORDER BY floor_id DESC', (chat_id,)).fetchall()

    def save_sync_gap(self, chat_id, floor_id, offset_id):
        self.conn.execute(
            'INSERT OR REPLACE INTO sync_gaps VALUES (?, ?, ?)',
            (chat_id, floor_id, offset_id))

    def delete_sync_gap(self, chat_id, floor_id):
        self.conn.execute(
            'DELETE FROM sync_gaps WHERE chat_id = ? AND floor_id = ?',
            (chat_id, floor_id))

    def _migrate(self):
        """Apply incremental schema migrations to DBs created by older versions."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        cols = [r[1] for r in cur.fetchall()]
        if "chat_id" not in cols:
            cur.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")
            self.conn.commit()
        if "thread_id" not in cols:
            cur.execute("ALTER TABLE messages ADD COLUMN thread_id INTEGER")
            self.conn.commit()
        cur.execute("PRAGMA table_info(users)")
        ucols = [r[1] for r in cur.fetchall()]
        if "bio" not in ucols:
            cur.execute("ALTER TABLE users ADD COLUMN bio TEXT")
            self.conn.commit()
        self._ensure_metadata_tables()
        self._ensure_fts_index()

    def _ensure_metadata_tables(self):
        """Create the chats / topics metadata tables if they don't exist yet."""
        cur = self.conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS chats (
            id INTEGER NOT NULL PRIMARY KEY,
            title TEXT,
            username TEXT,
            description TEXT,
            type TEXT,
            forum INTEGER DEFAULT 0,
            participants_count INTEGER,
            avatar TEXT,
            synced_at TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS topics (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            title TEXT,
            creator_id INTEGER,
            date TIMESTAMP,
            closed INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, thread_id),
            FOREIGN KEY(chat_id) REFERENCES chats(id)
        )""")
        self.conn.commit()

    def _ensure_fts_index(self):
        """Create the FTS5 full-text index over messages if it doesn't
        exist yet, backfilling it with the rows already in the table."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'")
        if cur.fetchone():
            return

        for s in fts_schema.split("##"):
            cur.execute(s)
        cur.execute("""INSERT INTO messages_fts (rowid, content, chat_id, thread_id)
            SELECT rowid, content, chat_id, thread_id FROM messages""")
        self.conn.commit()

    def _parse_date(self, d) -> str:
        return datetime.strptime(d, "%Y-%m-%dT%H:%M:%S%z")

    def get_last_message_id(self, chat_id=None) -> [int, datetime]:
        cur = self.conn.cursor()
        if chat_id is not None:
            cur.execute("""
                SELECT id, strftime('%Y-%m-%d 00:00:00', date) as "[timestamp]" FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC LIMIT 1
            """, (chat_id,))
        else:
            cur.execute("""
                SELECT id, strftime('%Y-%m-%d 00:00:00', date) as "[timestamp]" FROM messages
                ORDER BY id DESC LIMIT 1
            """)
        res = cur.fetchone()
        if not res:
            return 0, None

        id, date = res
        return id, date

    def get_timeline(self) -> Iterator[Month]:
        """
        Get the list of all unique yyyy-mm month groups and
        the corresponding message counts per period in chronological order.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT strftime('%Y-%m-%d 00:00:00', date) as "[timestamp]",
            COUNT(*) FROM messages AS count
            GROUP BY strftime('%Y-%m', date) ORDER BY date
        """)

        for r in cur.fetchall():
            date = pytz.utc.localize(r[0])
            if self.tz:
                date = date.astimezone(self.tz)

            yield Month(date=date,
                        slug=date.strftime("%Y-%m"),
                        label=date.strftime("%b %Y"),
                        count=r[1])

    def get_dayline(self, year, month, limit=500) -> Iterator[Day]:
        """
        Get the list of all unique yyyy-mm-dd days corresponding
        message counts and the page number of the first occurrence of 
        the date in the pool of messages for the whole month.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT strftime("%Y-%m-%d 00:00:00", date) AS "[timestamp]",
            COUNT(*), PAGE(rank, ?) FROM (
                SELECT ROW_NUMBER() OVER() as rank, date FROM messages
                WHERE strftime('%Y%m', date) = ? ORDER BY id
            )
            GROUP BY "[timestamp]";
        """, (limit, "{}{:02d}".format(year, month)))

        for r in cur.fetchall():
            date = pytz.utc.localize(r[0])
            if self.tz:
                date = date.astimezone(self.tz)

            yield Day(date=date,
                      slug=date.strftime("%Y-%m-%d"),
                      label=date.strftime("%d %b %Y"),
                      count=r[1],
                      page=r[2])

    def get_messages(self, year, month, last_id=0, limit=500) -> Iterator[Message]:
        date = "{}{:02d}".format(year, month)

        cur = self.conn.cursor()
        cur.execute("""
            SELECT messages.id, messages.chat_id, messages.thread_id, messages.type, messages.date, messages.edit_date,
            messages.content, messages.reply_to, messages.user_id,
            users.username, users.first_name, users.last_name, users.tags, users.avatar,
            media.id, media.type, media.url, media.title, media.description, media.thumb
            FROM messages
            LEFT JOIN users ON (users.id = messages.user_id)
            LEFT JOIN media ON (media.id = messages.media_id)
            WHERE strftime('%Y%m', date) = ?
            AND messages.id > ? ORDER by messages.id LIMIT ?
            """, (date, last_id, limit))

        for r in cur.fetchall():
            yield self._make_message(r)

    def get_message_count(self, year, month) -> int:
        date = "{}{:02d}".format(year, month)

        cur = self.conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM messages WHERE strftime('%Y%m', date) = ?
            """, (date,))

        total, = cur.fetchone()
        return total

    def search_messages(self, query, chat_id=None, thread_id=None, limit=100) -> Iterator[Message]:
        """
        Full-text search over message content using the FTS5 index.
        Optionally scope the search to a single chat or to a single
        forum topic (thread).
        """
        sql = """
            SELECT messages.id, messages.chat_id, messages.thread_id, messages.type, messages.date, messages.edit_date,
            messages.content, messages.reply_to, messages.user_id,
            users.username, users.first_name, users.last_name, users.tags, users.avatar,
            media.id, media.type, media.url, media.title, media.description, media.thumb
            FROM messages_fts
            JOIN messages ON (messages.rowid = messages_fts.rowid)
            LEFT JOIN users ON (users.id = messages.user_id)
            LEFT JOIN media ON (media.id = messages.media_id)
            WHERE messages_fts MATCH ?
            """
        params = [query]
        if chat_id is not None:
            sql += " AND messages.chat_id = ?"
            params.append(chat_id)
        if thread_id is not None:
            sql += " AND messages.thread_id = ?"
            params.append(thread_id)
        sql += " ORDER BY messages_fts.rank LIMIT ?"
        params.append(limit)

        cur = self.conn.cursor()
        cur.execute(sql, params)
        for r in cur.fetchall():
            yield self._make_message(r)

    def insert_user(self, u: User):
        """Insert a user and if they exist, update the fields."""
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO users (id, username, first_name, last_name, tags, avatar)
            VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT (id)
            DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
                last_name=excluded.last_name, tags=excluded.tags, avatar=excluded.avatar
            """, (u.id, u.username, u.first_name, u.last_name, " ".join(u.tags), u.avatar))

    def insert_media(self, m: Media):
        cur = self.conn.cursor()
        cur.execute("""INSERT OR REPLACE INTO media
            (id, type, url, title, description, thumb)
            VALUES(?, ?, ?, ?, ?, ?)""",
                    (m.id,
                     m.type,
                     m.url,
                     m.title,
                     m.description,
                     m.thumb)
                    )

    def insert_message(self, m: Message):
        cur = self.conn.cursor()
        cur.execute("""INSERT OR REPLACE INTO messages
            (id, chat_id, thread_id, type, date, edit_date, content, reply_to, user_id, media_id)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (m.id,
                     m.chat_id,
                     m.thread_id,
                     m.type,
                     m.date.strftime("%Y-%m-%d %H:%M:%S"),
                     m.edit_date.strftime(
                         "%Y-%m-%d %H:%M:%S") if m.edit_date else None,
                     m.content,
                     m.reply_to,
                     m.user.id,
                     m.media.id if m.media else None)
                    )

    def commit(self):
        """Commit pending writes to the DB."""
        self.conn.commit()

    def upsert_chat(self, c: Chat):
        """Insert a chat's metadata and if it exists, update the fields."""
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO chats
            (id, title, username, description, type, forum, participants_count, avatar, synced_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id)
            DO UPDATE SET title=excluded.title, username=excluded.username,
                description=excluded.description, type=excluded.type, forum=excluded.forum,
                participants_count=excluded.participants_count, avatar=excluded.avatar,
                synced_at=excluded.synced_at
            """, (c.id, c.title, c.username, c.description, c.type,
                  int(bool(c.forum)), c.participants_count, c.avatar,
                  c.synced_at.strftime("%Y-%m-%d %H:%M:%S") if c.synced_at else None))

    def get_chats(self) -> Iterator[Chat]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, title, username, description, type, forum,
            participants_count, avatar, synced_at FROM chats ORDER BY id
            """)
        for r in cur.fetchall():
            yield Chat(id=r[0], title=r[1], username=r[2], description=r[3],
                       type=r[4], forum=r[5], participants_count=r[6],
                       avatar=r[7], synced_at=r[8])

    def upsert_topic(self, t: Topic):
        """Insert a forum topic and if it exists, update the fields."""
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO topics
            (chat_id, thread_id, title, creator_id, date, closed)
            VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT (chat_id, thread_id)
            DO UPDATE SET title=excluded.title, creator_id=excluded.creator_id,
                date=excluded.date, closed=excluded.closed
            """, (t.chat_id, t.thread_id, t.title, t.creator_id,
                  t.date.strftime("%Y-%m-%d %H:%M:%S") if t.date else None,
                  int(bool(t.closed))))

    def update_topic(self, chat_id, thread_id, title=None, closed=None):
        """Update only the fields that are provided (topic edits carry
        changed fields only)."""
        cur = self.conn.cursor()
        cur.execute("""UPDATE topics
            SET title = COALESCE(?, title), closed = COALESCE(?, closed)
            WHERE chat_id = ? AND thread_id = ?
            """, (title, None if closed is None else int(bool(closed)),
                  chat_id, thread_id))

    def get_topics(self, chat_id=None) -> Iterator[Topic]:
        cur = self.conn.cursor()
        if chat_id is not None:
            cur.execute("""
                SELECT chat_id, thread_id, title, creator_id, date, closed
                FROM topics WHERE chat_id = ? ORDER BY thread_id
                """, (chat_id,))
        else:
            cur.execute("""
                SELECT chat_id, thread_id, title, creator_id, date, closed
                FROM topics ORDER BY chat_id, thread_id
                """)
        for r in cur.fetchall():
            date = pytz.utc.localize(r[4]) if r[4] else None
            yield Topic(chat_id=r[0], thread_id=r[1], title=r[2],
                        creator_id=r[3], date=date, closed=r[5])

    def get_missing_topic_ids(self, chat_id) -> [int]:
        """
        Returns the thread IDs that exist in the chat's messages but have
        no row in the topics table, i.e. topics created before topic
        tracking existed.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT thread_id FROM messages
            WHERE chat_id = ? AND thread_id IS NOT NULL
            AND thread_id NOT IN (SELECT thread_id FROM topics WHERE chat_id = ? AND title IS NOT NULL)
            """, (chat_id, chat_id))
        return [r[0] for r in cur.fetchall()]

    def get_users_without_bio(self) -> [int]:
        """Returns the IDs of users whose bio has never been fetched."""
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE bio IS NULL")
        return [r[0] for r in cur.fetchall()]

    def update_user_bio(self, user_id, bio):
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, user_id))

    def _make_message(self, m) -> Message:
        """Makes a Message() object from an SQL result tuple."""
        id, chat_id, thread_id, typ, date, edit_date, content, reply_to, \
            user_id, username, first_name, last_name, tags, avatar, \
            media_id, media_type, media_url, media_title, media_description, media_thumb = m

        md = None
        if media_id:
            desc = media_description
            if media_type == "poll":
                desc = json.loads(media_description)

            md = Media(id=media_id,
                       type=media_type,
                       url=media_url,
                       title=media_title,
                       description=desc,
                       thumb=media_thumb)

        date = pytz.utc.localize(date) if date else None
        edit_date = pytz.utc.localize(edit_date) if edit_date else None

        if self.tz:
            date = date.astimezone(self.tz) if date else None
            edit_date = edit_date.astimezone(self.tz) if edit_date else None

        return Message(id=id,
                       type=typ,
                       date=date,
                       edit_date=edit_date,
                       content=content,
                       reply_to=reply_to,
                       user=User(id=user_id,
                                 username=username,
                                 first_name=first_name,
                                 last_name=last_name,
                                 tags=tags,
                                 avatar=avatar),
                        media=md,
                        chat_id=chat_id,
                        thread_id=thread_id)
