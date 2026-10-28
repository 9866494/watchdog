import pytest

from tgarchive.db import DB
from tgarchive.sync import Sync
from test_sync import FakeMessage, FakeSync, FakeUser, make_config, make_message_row


def feed(count):
    return [FakeMessage(i, FakeUser(1, 'author')) for i in range(1, count + 1)]


def test_recent_messages_of_all_chats_precede_history(tmp_path):
    db = DB(str(tmp_path / 'data.sqlite'))
    sync = FakeSync(make_config(tmp_path, chats=[1, 2], fetch_batch_size=2),
                    db, {1: feed(5), 2: feed(5)})
    sync.sync()

    assert sync.fetch_calls[:2] == [(1, 0, None), (2, 0, None)]
    assert sync.fetch_calls[2:4] == [(1, 4, None), (2, 4, None)]
    assert db.conn.execute('select count(*) from messages').fetchone()[0] == 10
    assert db.get_sync_gaps(1) == db.get_sync_gaps(2) == []


def test_restart_fills_gap_even_after_new_messages_arrive(tmp_path):
    path = str(tmp_path / 'data.sqlite')
    db = DB(path)
    config = make_config(tmp_path, chats=[1], fetch_batch_size=2, fetch_limit=2)
    FakeSync(config, db, {1: feed(6)}).sync()
    assert db.get_sync_gaps(1) == [(0, 5)]
    db.conn.close()

    db = DB(path)
    FakeSync(config, db, {1: feed(8)}).sync()
    assert db.get_sync_gaps(1) == [(6, 7), (0, 5)]

    config['fetch_limit'] = 0
    FakeSync(config, db, {1: feed(8)}).sync()
    assert [r[0] for r in db.conn.execute('select id from messages order by id')] == list(range(1, 9))
    assert db.get_sync_gaps(1) == []


def test_migration_keeps_existing_old_history_and_fills_newer_gap(tmp_path):
    db = DB(str(tmp_path / 'data.sqlite'))
    for i in range(1, 4):
        db.insert_message(make_message_row(i, 1))
    db.commit()
    sync = FakeSync(make_config(tmp_path, chats=[1], fetch_batch_size=2), db, {1: feed(8)})
    sync.sync()
    assert db.conn.execute('select count(*) from messages').fetchone()[0] == 8
    assert (1, 7, None) in sync.fetch_calls
    assert db.get_sync_gaps(1) == []


def test_failed_page_rolls_back_messages_and_cursor(tmp_path):
    db = DB(str(tmp_path / 'data.sqlite'))
    sync = FakeSync(make_config(tmp_path), db, {1: feed(4)})
    original = db.insert_message

    def fail(message):
        if message.id == 3:
            raise RuntimeError('Ошибка записи')
        original(message)

    db.insert_message = fail
    with pytest.raises(RuntimeError):
        with db.conn:
            sync._sync_newest_page(1, 0, 0, 2)
    assert db.conn.execute('select count(*) from messages').fetchone()[0] == 0
    assert db.get_sync_gaps(1) == []


def test_failed_chat_does_not_block_others(tmp_path):
    db = DB(str(tmp_path / 'data.sqlite'))
    sync = FakeSync(make_config(tmp_path, chats=[1, 2]), db, {2: feed(3)})
    original = sync._fetch_messages

    def fetch(group, *args, **kwargs):
        if group == 1:
            raise RuntimeError('Ошибка Telegram')
        return original(group, *args, **kwargs)

    sync._fetch_messages = fetch
    with pytest.raises(RuntimeError):
        sync.sync()
    assert db.conn.execute('select count(*) from messages where chat_id=2').fetchone()[0] == 3


def test_transport_uses_exclusive_descending_bounds():
    class Client:
        def get_messages(self, group, **kwargs):
            assert group == 42
            assert kwargs['reverse'] is False
            assert kwargs['offset_id'] == 20
            assert kwargs['min_id'] == 5
            assert kwargs['limit'] == 2
            return []

    sync = Sync.__new__(Sync)
    sync.config = {'fetch_batch_size': 5000}
    sync.client = Client()
    assert sync._fetch_messages(42, 20, newest_first=True, min_id=5, limit=2) == []


def test_topic_fields_keep_latest_edits_across_restart(tmp_path):
    path = str(tmp_path / 'data.sqlite')
    db = DB(path)
    db.record_topic_action(1, 10, 30, title='Новое название')
    db.record_topic_action(1, 10, 25, closed=True)
    db.commit()
    db.conn.close()

    db = DB(path)
    db.record_topic_action(1, 10, 20, title='Старое название', closed=False)
    db.record_topic_action(1, 10, 10, title='Первое название', closed=False)
    topic = next(db.get_topics(1))
    assert topic.title == 'Новое название'
    assert topic.closed == 1
