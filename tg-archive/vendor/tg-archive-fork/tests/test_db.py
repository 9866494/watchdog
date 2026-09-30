import datetime
import os
import sqlite3

import pytz

from tgarchive.db import DB, Message, User

DATE = pytz.utc.localize(datetime.datetime(2024, 1, 15, 12, 0, 0))


def make_user():
    return User(id=1, username="alice", first_name="Alice",
                last_name=None, tags=[], avatar=None)


def make_message(id, chat_id, content="hello"):
    return Message(id=id,
                   type="message",
                   date=DATE,
                   edit_date=None,
                   content=content,
                   reply_to=None,
                   user=make_user(),
                   media=None,
                   chat_id=chat_id)


def new_db(tmp_path):
    return DB(os.path.join(str(tmp_path), "test.sqlite"))


def test_messages_table_has_chat_id(tmp_path):
    db = new_db(tmp_path)
    cols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(messages)").fetchall()]
    assert "chat_id" in cols


def test_same_message_id_in_multiple_chats(tmp_path):
    """Telegram message IDs are per-chat; the composite PK must keep both."""
    db = new_db(tmp_path)
    db.insert_message(make_message(1, -1001, "a"))
    db.insert_message(make_message(1, -1002, "b"))
    db.commit()

    rows = db.conn.execute(
        "SELECT id, chat_id FROM messages ORDER BY chat_id").fetchall()
    assert rows == [(1, -1002), (1, -1001)]


def test_upsert_same_id_and_chat(tmp_path):
    db = new_db(tmp_path)
    db.insert_message(make_message(1, -1001, "old"))
    db.insert_message(make_message(1, -1001, "new"))
    db.commit()

    rows = db.conn.execute(
        "SELECT content FROM messages").fetchall()
    assert rows == [("new",)]


def test_get_last_message_id_per_chat(tmp_path):
    db = new_db(tmp_path)
    db.insert_message(make_message(1, -1001))
    db.insert_message(make_message(2, -1001))
    db.insert_message(make_message(1, -1002))
    db.commit()

    assert db.get_last_message_id()[0] == 2
    assert db.get_last_message_id(chat_id=-1001)[0] == 2
    assert db.get_last_message_id(chat_id=-1002)[0] == 1
    assert db.get_last_message_id(chat_id=-9999)[0] == 0


def test_get_messages_returns_chat_id(tmp_path):
    db = new_db(tmp_path)
    db.insert_message(make_message(1, -1001))
    db.insert_message(make_message(1, -1002))
    db.commit()

    msgs = list(db.get_messages(2024, 1))
    assert len(msgs) == 2
    assert {m.chat_id for m in msgs} == {-1001, -1002}


def test_legacy_db_gets_chat_id_migration(tmp_path):
    """A DB created before the chat_id schema change must still open."""
    path = os.path.join(str(tmp_path), "legacy.sqlite")
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE messages (
        id INTEGER NOT NULL PRIMARY KEY, type TEXT NOT NULL,
        date TIMESTAMP NOT NULL, edit_date TIMESTAMP, content TEXT,
        reply_to INTEGER, user_id INTEGER, media_id INTEGER)""")
    c.execute("""CREATE TABLE users (
        id INTEGER NOT NULL PRIMARY KEY, username TEXT, first_name TEXT,
        last_name TEXT, tags TEXT, avatar TEXT)""")
    c.execute("""CREATE TABLE media (
        id INTEGER NOT NULL PRIMARY KEY, type TEXT, url TEXT, title TEXT,
        description TEXT, thumb TEXT)""")
    c.commit()
    c.close()

    db = DB(path)
    cols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(messages)").fetchall()]
    assert "chat_id" in cols

    # And writes with chat_id work on the migrated DB.
    db.insert_message(make_message(5, -1003))
    db.commit()
    assert db.get_last_message_id(chat_id=-1003)[0] == 5
