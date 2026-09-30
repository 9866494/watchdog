"""Tests for chat / topic metadata persistence and user bio enrichment."""

import os
import sqlite3

import telethon.tl.types

from tgarchive.db import DB, Message, Topic, User
from tgarchive.sync import Sync

from test_sync import DATE, FakeMessage, FakeSync, FakeUser, make_config
from test_threads import FakeReplyHeader, TopicMessage

# --- helpers ---------------------------------------------------------------


def make_message(id, chat_id, thread_id=None, content="seed"):
    return Message(id=id,
                   type="message",
                   date=DATE,
                   edit_date=None,
                   content=content,
                   reply_to=None,
                   user=User(id=1, username="alice", first_name=None,
                             last_name=None, tags=[], avatar=None),
                   media=None,
                   chat_id=chat_id,
                   thread_id=thread_id)


def make_topic(title):
    return telethon.tl.types.MessageActionTopicCreate(title=title, icon_color=0)


# --- chat metadata ---------------------------------------------------------


def test_upsert_chat_maps_supergroup_type(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    s = Sync.__new__(Sync)
    s.config = make_config(tmp_path)
    s.db = db

    ch = telethon.tl.types.Channel(
        id=1001, title="Test Group", photo=telethon.tl.types.ChatPhotoEmpty(),
        date=DATE, participants_count=24, broadcast=False, megagroup=True,
        forum=True, username="test_group")
    s._upsert_chat(ch)

    rows = list(db.get_chats())
    assert len(rows) == 1
    assert rows[0].id == 1001
    assert rows[0].title == "Test Group"
    assert rows[0].type == "supergroup"
    assert rows[0].forum == 1
    assert rows[0].participants_count == 24
    assert rows[0].username == "test_group"


def test_upsert_chat_maps_channel_and_chat_types(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    s = Sync.__new__(Sync)
    s.config = make_config(tmp_path)
    s.db = db

    channel = telethon.tl.types.Channel(
        id=1002, title="Broadcast", photo=telethon.tl.types.ChatPhotoEmpty(),
        date=DATE, broadcast=True, megagroup=False)
    s._upsert_chat(channel)

    rows = {r.id: r for r in db.get_chats()}
    assert rows[1002].type == "channel"
    assert rows[1002].forum == 0


def test_sync_upserts_chat_metadata(tmp_path):
    """A sync run stores a chat row even when no metadata is attached."""
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    alice = FakeUser(1, "alice")
    s = FakeSync(make_config(tmp_path, chats=[-1001]), db,
                 {-1001: [FakeMessage(1, alice, "hello")]})
    s.sync()

    rows = list(db.get_chats())
    assert len(rows) == 1
    assert rows[0].id == -1001
    assert rows[0].title == "fake chat"


# --- topic capture ---------------------------------------------------------


def test_sync_captures_topic_create_from_action(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    alice = FakeUser(1, "alice")
    feed = {
        -1001: [
            TopicMessage(1, alice, "General", action=make_topic("General")),
            TopicMessage(2, alice, "in topic",
                         reply_to=FakeReplyHeader(1, forum_topic=True)),
        ],
    }
    s = FakeSync(make_config(tmp_path, chats=[-1001]), db, feed)
    s.sync()

    topics = list(db.get_topics(-1001))
    assert len(topics) == 1
    assert topics[0].thread_id == 1
    assert topics[0].title == "General"
    assert topics[0].creator_id == 1


def test_sync_applies_topic_edit(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    alice = FakeUser(1, "alice")
    edit = telethon.tl.types.MessageActionTopicEdit(title="Renamed")
    feed = {
        -1001: [
            TopicMessage(1, alice, "root", action=make_topic("Original")),
            TopicMessage(5, alice, "edited",
                         reply_to=FakeReplyHeader(1, forum_topic=True),
                         action=edit),
        ],
    }
    s = FakeSync(make_config(tmp_path, chats=[-1001]), db, feed)
    s.sync()

    topics = list(db.get_topics(-1001))
    assert topics[0].title == "Renamed"


# --- topic backfill --------------------------------------------------------


def test_backfill_fills_missing_topic_titles(tmp_path):
    """Topics that predate topic tracking are filled by message-id lookup."""
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    # Pre-existing messages with thread IDs but no topics rows.
    db.insert_message(make_message(10, -1001, thread_id=155))
    db.insert_message(make_message(11, -1001, thread_id=200))
    db.commit()

    alice = FakeUser(1, "alice")
    s = FakeSync(make_config(tmp_path, chats=[-1001]), db, {-1001: []})
    s.backfill_feed = {
        -1001: [
            TopicMessage(155, alice, "", action=make_topic("Pre-created A")),
            TopicMessage(200, alice, "", action=make_topic("Pre-created B")),
            # An unrelated ID must not be upserted.
            TopicMessage(999, alice, "", action=make_topic("Not ours")),
        ],
    }
    s.sync()

    topics = {t.thread_id: t.title for t in db.get_topics(-1001)}
    assert topics == {155: "Pre-created A", 200: "Pre-created B"}


# --- user bios -------------------------------------------------------------


def test_bios_not_fetched_when_flag_off(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    db.conn.execute("INSERT INTO users (id, username) VALUES (1, 'a')")
    db.commit()

    s = FakeSync(make_config(tmp_path, chats=[-1001]), db, {-1001: []})
    s.sync()

    assert s.bio_calls == []


def test_bios_fetched_only_for_users_without_bio(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    db.conn.execute(
        "INSERT INTO users (id, username, bio) VALUES (1,'a','known'), (2,'b',NULL), (3,'c',NULL)")
    db.commit()

    s = FakeSync(make_config(tmp_path, chats=[-1001], fetch_user_bios=True),
                 db, {-1001: []})
    s.bios = {2: "bio b", 3: None}
    s.sync()

    assert sorted(s.bio_calls) == [2, 3]
    bios = dict(db.conn.execute("SELECT id, bio FROM users").fetchall())
    assert bios == {1: "known", 2: "bio b", 3: ""}


def test_bios_not_refetched_after_first_run(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    db.conn.execute("INSERT INTO users (id, username, bio) VALUES (1,'a',NULL)")
    db.commit()

    cfg = make_config(tmp_path, chats=[-1001], fetch_user_bios=True)
    s = FakeSync(cfg, db, {-1001: []})
    s.bios = {1: "set once"}
    s.sync()
    assert s.bio_calls == [1]

    s2 = FakeSync(cfg, db, {-1001: []})
    s2.bios = {1: "again"}
    s2.sync()
    assert s2.bio_calls == []


# --- DB layer round-trip ---------------------------------------------------


def test_upsert_and_update_topic(tmp_path):
    db = DB(os.path.join(str(tmp_path), "t.sqlite"))
    db.upsert_topic(Topic(chat_id=-1001, thread_id=5, title="A",
                          creator_id=1, date=DATE, closed=0))
    db.commit()
    topics = list(db.get_topics(-1001))
    assert topics[0].title == "A"

    db.update_topic(-1001, 5, title="B", closed=True)
    db.commit()
    topics = list(db.get_topics(-1001))
    assert topics[0].title == "B"
    assert topics[0].closed == 1


# --- migration ---------------------------------------------------------------


def test_legacy_db_gets_metadata_tables_and_bio(tmp_path):
    """A DB from the multi-chat era (no chats/topics, no users.bio) is
    migrated in place."""
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
    c.commit()
    c.close()

    db = DB(path)
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "chats" in tables
    assert "topics" in tables
    ucols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(users)").fetchall()]
    assert "bio" in ucols
