import datetime
import os

import pytest
import pytz

from tgarchive import parse_chat_ref, parse_chats
from tgarchive.db import DB
from tgarchive.sync import Sync

DATE = pytz.utc.localize(datetime.datetime(2024, 1, 15, 12, 0, 0))


# ---------------------------------------------------------------------------
# URL / reference parsing
# ---------------------------------------------------------------------------

def test_parse_chat_ref_tme_url():
    assert parse_chat_ref("https://t.me/group") == "@group"


def test_parse_chat_ref_url_variants():
    assert parse_chat_ref("http://t.me/group") == "@group"
    assert parse_chat_ref("t.me/group") == "@group"
    assert parse_chat_ref("https://www.t.me/group") == "@group"
    assert parse_chat_ref("https://telegram.me/group") == "@group"


def test_parse_chat_ref_url_trailing_slash_and_query():
    assert parse_chat_ref("https://t.me/group/") == "@group"
    assert parse_chat_ref("https://t.me/group?start=123") == "@group"
    assert parse_chat_ref("https://t.me/group#fragment") == "@group"


def test_parse_chat_ref_url_preserves_case():
    assert parse_chat_ref("https://t.me/MyGroup") == "@MyGroup"


def test_parse_chat_ref_invite_link_rejected():
    with pytest.raises(ValueError):
        parse_chat_ref("https://t.me/+abc123")


def test_parse_chat_ref_empty_rejected():
    with pytest.raises(ValueError):
        parse_chat_ref("")
    with pytest.raises(ValueError):
        parse_chat_ref("   ")


def test_parse_chat_ref_chat_id():
    assert parse_chat_ref("-100123") == -100123
    assert parse_chat_ref(-100123) == -100123
    assert parse_chat_ref(" 42 ") == 42


def test_parse_chat_ref_username_and_display_name():
    assert parse_chat_ref("@mygroup") == "@mygroup"
    assert parse_chat_ref("My Group") == "My Group"


def test_parse_chats_normalizes_urls():
    assert parse_chats("-100123,https://t.me/foo,@bar,My Group") == \
        [-100123, "@foo", "@bar", "My Group"]


# ---------------------------------------------------------------------------
# Fakes for dialog-based resolution and filtering
# ---------------------------------------------------------------------------

class FakeEntity:
    def __init__(self, id, username=None, title=None):
        self.id = id
        self.username = username
        self.title = title


class FakeDialog:
    """Bare minimum surface of a Telethon dialog consumed by Sync."""

    def __init__(self, entity, is_group=False, is_channel=False):
        self.entity = entity
        self.title = entity.title
        self.is_group = is_group
        self.is_channel = is_channel
        # Mimic utils.get_peer_id(): channels get a -100 prefix,
        # basic groups are negated, users stay as-is.
        if is_channel:
            self.id = int("-100{}".format(entity.id))
        elif is_group:
            self.id = -entity.id
        else:
            self.id = entity.id


def make_dialogs():
    return [
        FakeDialog(FakeEntity(1001, username="Engineering",
                              title="Engineering Team"), is_channel=True),
        FakeDialog(FakeEntity(2002, title="Random Chat"), is_group=True),
        # Not a group / channel: must never be picked by the filter pool.
        FakeDialog(FakeEntity(3003, username="alice", title="Team Alice")),
    ]


def make_sync(chats=None, group="", dialogs=None):
    """Sync without a client: config + cached dialogs only."""
    s = Sync.__new__(Sync)
    s.config = {"chats": chats or [], "group": group}
    s._dialogs = dialogs if dialogs is not None else []
    return s


# ---------------------------------------------------------------------------
# Dialog matching (resolution)
# ---------------------------------------------------------------------------

def test_match_dialog_by_plain_id():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog(1001).entity.id == 1001


def test_match_dialog_by_marked_id():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog(-1001001).entity.id == 1001
    assert s._match_dialog(-2002).entity.id == 2002


def test_match_dialog_by_marked_id_string():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog("-1001001").entity.id == 1001


def test_match_dialog_by_username_case_insensitive():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog("@engineering").entity.id == 1001


def test_match_dialog_by_url():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog("https://t.me/Engineering").entity.id == 1001


def test_match_dialog_by_display_name_case_insensitive():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog("random chat").entity.id == 2002


def test_match_dialog_no_match():
    s = make_sync(dialogs=make_dialogs())
    assert s._match_dialog("@nobody") is None
    assert s._match_dialog("No Such Chat") is None
    assert s._match_dialog(-1009999) is None


# ---------------------------------------------------------------------------
# Regex filtering
# ---------------------------------------------------------------------------

def test_select_chats_filter_restricts_explicit_chats():
    s = make_sync(chats=[-1001001, -2002], dialogs=make_dialogs())
    selected = s._select_chats(".*Engineering.*")
    assert [e.id for e in selected] == [1001]


def test_select_chats_filter_no_explicit_chats_matches_all_dialogs():
    s = make_sync(dialogs=make_dialogs())
    selected = s._select_chats(".*Team.*")
    # The user dialog "Team Alice" must not be in the pool.
    assert [e.id for e in selected] == [1001]


def test_select_chats_filter_uses_re_search():
    # A bare substring pattern (no wildcards) must match via re.search.
    s = make_sync(dialogs=make_dialogs())
    selected = s._select_chats("Random")
    assert [e.id for e in selected] == [2002]


def test_select_chats_filter_no_match_returns_empty():
    s = make_sync(chats=[-1001001], dialogs=make_dialogs())
    assert s._select_chats(".*NoSuchChat.*") == []


def test_select_chats_no_filter_keeps_legacy_refs():
    s = make_sync(chats=[-1001001, "@engineering"])
    assert s._select_chats() == [-1001001, "@engineering"]


def test_select_chats_filter_skips_unresolvable_chats():
    s = make_sync(chats=[-1001001, -1009999], dialogs=make_dialogs())
    selected = s._select_chats(".*")
    assert [e.id for e in selected] == [1001]


# ---------------------------------------------------------------------------
# Labels and resolution helpers
# ---------------------------------------------------------------------------

def test_resolve_chat_id_entity_passthrough():
    s = make_sync()
    assert s._resolve_chat_id(FakeEntity(1001)) == 1001


def test_chat_label_prefers_title_then_username_then_id():
    s = make_sync()
    assert s._chat_label(FakeEntity(1, title="My Chat")) == "My Chat"
    assert s._chat_label(FakeEntity(1, username="chat")) == "@chat"
    assert s._chat_label(FakeEntity(1)) == "1"
    assert s._chat_label("@whatever") == "@whatever"


# ---------------------------------------------------------------------------
# Dry run and filtered sync (stubbed transport)
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, id, username):
        self.id = id
        self.username = username
        self.first_name = None
        self.last_name = None
        self.scam = False
        self.fake = False


class FakeMessage:
    def __init__(self, id, sender, text="hello", date=DATE):
        self.id = id
        self.date = date
        self.edit_date = None
        self.media = None
        self.action = None
        self.raw_text = text
        self.reply_to = None
        self.sender = sender
        self.chat = None


class FakeSync(Sync):
    """Sync with the Telegram transport stubbed out (no client, no network)."""

    def __init__(self, config, db, feed, dialogs=None):
        self.config = config
        self.db = db
        self.client = None
        self._dialogs = dialogs if dialogs is not None else []
        self.feed = {g: list(msgs) for g, msgs in feed.items()}
        self.fetch_calls = []

    def _get_group_id(self, group):
        return int(group)

    def _fetch_messages(self, group, offset_id, ids=None, **kwargs):
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


def test_dry_run_does_not_sync(tmp_path):
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    s = FakeSync(make_config(tmp_path, chats=[-1001, -1002]), db,
                 {-1001: [FakeMessage(1, FakeUser(1, "alice"))]})
    assert s.sync(dry_run=True) == 0
    assert s.fetch_calls == []
    assert db.conn.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_dry_run_with_filter(tmp_path):
    s = make_sync(dialogs=make_dialogs())
    s.config["use_takeout"] = False
    assert s.sync(chat_filter=".*Engineering.*", dry_run=True) == 0


def test_sync_with_filter_only_syncs_matching_chats(tmp_path):
    db = DB(os.path.join(str(tmp_path), "data.sqlite"))
    alice = FakeUser(1, "alice")
    feed = {
        1001001: [FakeMessage(1, alice, "e1")],
        2002: [FakeMessage(1, alice, "r1")],
    }
    dialogs = [
        FakeDialog(FakeEntity(1001001, title="Engineering Team"),
                   is_channel=True),
        FakeDialog(FakeEntity(2002, title="Random Chat"), is_group=True),
    ]
    s = FakeSync(make_config(tmp_path, chats=[-1001001001, -2002]), db,
                 feed, dialogs=dialogs)
    s.sync(chat_filter=".*Engineering.*")

    rows = db.conn.execute(
        "SELECT chat_id, content FROM messages").fetchall()
    assert rows == [(1001001, "e1")]
    assert {c[0] for c in s.fetch_calls} == {1001001}
