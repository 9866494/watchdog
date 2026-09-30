from tgarchive import parse_chats


def test_parse_chats_numeric_ids():
    assert parse_chats("-100123,-100456") == [-100123, -100456]


def test_parse_chats_trims_whitespace():
    assert parse_chats(" -100123 , -100456 ") == [-100123, -100456]


def test_parse_chats_mixed_ids_and_usernames():
    assert parse_chats("-100123,@mychannel") == [-100123, "@mychannel"]


def test_parse_chats_empty_input():
    assert parse_chats(None) == []
    assert parse_chats("") == []
    assert parse_chats(",, ,") == []
