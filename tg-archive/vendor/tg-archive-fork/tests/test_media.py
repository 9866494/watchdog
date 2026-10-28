from types import SimpleNamespace

import pytest
from telethon.tl import types

from tgarchive.sync import Sync
from test_sync import DATE, FakeMessage, FakeSync, FakeUser, make_config


@pytest.mark.parametrize('page', [
    types.WebPagePending(id=1, date=DATE),
    types.WebPageEmpty(id=1),
])
def test_incomplete_preview_preserves_message(tmp_path, page):
    message = FakeMessage(10, FakeUser(1, 'author'), text='Текст со ссылкой')
    message.media = types.MessageMediaWebPage(webpage=page)
    sync = FakeSync(make_config(tmp_path), None, {42: [message]})

    messages = list(sync._get_messages(42, 0))

    assert len(messages) == 1
    assert messages[0].content == 'Текст со ссылкой'
    assert messages[0].media is None


def test_complete_preview_keeps_metadata():
    page = types.WebPage(id=1, url='https://example.com',
                         display_url='example.com', hash=0,
                         title='Вакансия', description='Описание')
    message = SimpleNamespace(id=10, media=types.MessageMediaWebPage(webpage=page))

    media = Sync.__new__(Sync)._get_media(message)

    assert media.url == 'https://example.com'
    assert media.title == 'Вакансия'
    assert media.description == 'Описание'
