"""Tests for forum topic (thread) support: thread_id extraction from
Telethon messages, storage in the DB, and topic-scoped FTS5 search."""

import os
import sqlite3

import telethon.tl.types

from tgarchive.db import DB, Message, User
from tgarchive.sync import Sync

from test_sync import DATE, FakeMessage, FakeSync, FakeUser, make_config


class FakeReplyHeader:
    """Bare minimum surface of Telethon's MessageReplyHeader."""

    def __init__(self, reply_to_msg_id, reply_to_top_id=None, forum_topic=False):
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to_top_id = reply_to_top_id
        self.forum_topic = forum_topic


class TopicMessage(FakeMessage):
    """FakeMessage with a configurable reply header and action."""

    def __init__(self, id, sender, text="hello", date=DATE, reply_to=None, action=None):
        super().__init__(id, sender, text, date)
        self.reply_to = reply_to
        self.action = action

    @property
    def reply_to_msg_id(self):
        # Mirrors Telethon's Message.reply_to_msg_id shortcut property.
        return self.reply_to.reply_to_msg_id if self.reply_to else None


def make_topic_create_action():
    return telethon.tl.types.MessageActionTopicCreate(title="General", icon_color=0)


# --- thread_id extraction ---------------------------------------------------


def test_topic_root_message_thread_id_is_its_own_id():
    """The message that creates a topic is the thread root itself."""
    s = Sync.__new__(Sync)
    m = TopicMessage(10, None, "topic created", action=make_topic_create_action())
    assert s._get_thread_id(m) == 10


def test_reply_in_topic_resolves_thread_root():
    s = Sync.__new__(Sync)
    m = TopicMessage(11, None, "deep reply",
                     reply_to=FakeReplyHeader(5, reply_to_top_id=10, forum_topic=True))
    assert s._get_thread_id(m) == 10


def test_direct_reply_to_topic_root_uses_reply_to_msg_id():
    """Direct replies to the topic root have no reply_to_top_id."""
    s = Sync.__new__(Sync)
    m = TopicMessage(12, None, "first reply",
                     reply_to=FakeReplyHeader(10, forum_topic=True))
    assert s._get_thread_id(m) == 10


def test_plain_reply_without_topics_has_no_thread():
    """Groups without topics: forum_topic is unset, thread_id stays NULL."""
    s = Sync.__new__(Sync)
    m = TopicMessage(13, None, "plain reply", reply_to=FakeReplyHeader(7))
    assert s._get_thread_id(m) is None


def test_message_without_reply_has_no_thread():
    s = Sync.__new__(Sync)
    assert s._get_thread_id(TopicMessage(14, None, "hello")) is None


# --- sync end-to-end ---------------------------------------------------------


def test_sync_stores_thread_id(tmp_path):
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    alice = FakeUser(1, "alice")
    feed = {
        -1001: [
            TopicMessage(1, alice, "General", action=make_topic_create_action()),
            TopicMessage(2, alice, "in topic",
                         reply_to=FakeReplyHeader(1, forum_topic=True)),
            TopicMessage(3, alice, "deeper in topic",
                         reply_to=FakeReplyHeader(2, reply_to_top_id=1, forum_topic=True)),
            FakeMessage(4, alice, "no topic here"),
        ],
    }

    s = FakeSync(make_config(tmp_path, chats=[-1001]), db, feed)
    s.sync()

    rows = db.conn.execute(
        "SELECT id, thread_id FROM messages ORDER BY id").fetchall()
    assert rows == [(1, 1), (2, 1), (3, 1), (4, None)]


# --- DB schema and round-trip -------------------------------------------------


def make_user():
    return User(id=1, username="alice", first_name=None,
                last_name=None, tags=[], avatar=None)


def make_message(id, chat_id, content="hello", thread_id=None):
    return Message(id=id,
                   type="message",
                   date=DATE,
                   edit_date=None,
                   content=content,
                   reply_to=None,
                   user=make_user(),
                   media=None,
                   chat_id=chat_id,
                   thread_id=thread_id)


def test_messages_table_has_thread_id(tmp_path):
    db = DB(os.path.join(str(tmp_path), "test.sqlite"))
    cols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(messages)").fetchall()]
    assert "thread_id" in cols


def test_get_messages_returns_thread_id(tmp_path):
    db = DB(os.path.join(str(tmp_path), "test.sqlite"))
    db.insert_message(make_message(1, -1001, "topic root", thread_id=1))
    db.insert_message(make_message(2, -1001, "plain"))
    db.commit()

    msgs = list(db.get_messages(2024, 1))
    assert {m.id: m.thread_id for m in msgs} == {1: 1, 2: None}


# --- FTS5 topic-scoped search ---------------------------------------------------


def test_fts_search_scoped_by_thread_id(tmp_path):
    db = DB(os.path.join(str(tmp_path), "test.sqlite"))
    db.insert_message(make_message(1, -1001, "python asyncio guide", thread_id=1))
    db.insert_message(make_message(2, -1001, "python flask guide", thread_id=2))
    db.insert_message(make_message(3, -1002, "python asyncio production"))
    db.commit()

    assert sorted(m.id for m in db.search_messages("python")) == [1, 2, 3]
    assert [m.id for m in db.search_messages("python", thread_id=1)] == [1]
    assert [m.id for m in db.search_messages("python", thread_id=2)] == [2]
    assert [m.id for m in db.search_messages("python", chat_id=-1002)] == [3]


def test_fts_reindexes_on_replace(tmp_path):
    """INSERT OR REPLACE must not leave stale or duplicate FTS entries."""
    db = DB(os.path.join(str(tmp_path), "test.sqlite"))
    db.insert_message(make_message(1, -1001, "original wording"))
    db.commit()
    db.insert_message(make_message(1, -1001, "revised wording"))
    db.commit()

    assert list(db.search_messages("original")) == []
    assert [m.content for m in db.search_messages("revised")] == ["revised wording"]
    assert len(list(db.search_messages("wording"))) == 1


def test_legacy_db_gets_thread_id_and_backfilled_fts(tmp_path):
    """A DB from the multi-chat era (no thread_id, no FTS) must be
    migrated and its existing rows must become searchable."""
    path = os.path.join(str(tmp_path), "legacy.sqlite")
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE messages (
        id INTEGER NOT NULL, chat_id INTEGER, type TEXT NOT NULL,
        date TIMESTAMP NOT NULL, edit_date TIMESTAMP, content TEXT,
        reply_to INTEGER, user_id INTEGER, media_id INTEGER,
        PRIMARY KEY (id, chat_id))""")
    c.execute("""CREATE TABLE users (
        id INTEGER NOT NULL PRIMARY KEY, username TEXT, first_name TEXT,
        last_name TEXT, tags TEXT, avatar TEXT)""")
    c.execute("""CREATE TABLE media (
        id INTEGER NOT NULL PRIMARY KEY, type TEXT, url TEXT, title TEXT,
        description TEXT, thumb TEXT)""")
    c.execute("INSERT INTO users (id, username) VALUES (1, 'alice')")
    c.execute("""INSERT INTO messages (id, chat_id, type, date, content, user_id)
        VALUES (9, -1001, 'message', '2024-01-15 12:00:00', 'legacy searchable', 1)""")
    c.commit()
    c.close()

    db = DB(path)
    cols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(messages)").fetchall()]
    assert "thread_id" in cols

    # Pre-existing rows are indexed by the migration backfill.
    assert [m.id for m in db.search_messages("legacy")] == [9]
