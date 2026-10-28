import pytest
from telethon.tl import types

from test_sync import FakeMessage, FakeSync, FakeUser, make_config


@pytest.mark.parametrize('header, expected', [
    (None, None),
    (types.MessageReplyHeader(reply_to_msg_id=5), 5),
    (types.MessageReplyStoryHeader(peer=types.PeerUser(1), story_id=5), None),
])
def test_reply_headers_preserve_message(tmp_path, header, expected):
    message = FakeMessage(10, FakeUser(1, 'author'), text='Ответ')
    message.reply_to = header
    sync = FakeSync(make_config(tmp_path), None, {42: [message]})

    messages = list(sync._get_messages(42, 0))

    assert len(messages) == 1
    assert messages[0].content == 'Ответ'
    assert messages[0].reply_to == expected
    assert messages[0].thread_id is None
