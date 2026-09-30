import argparse
import logging
import os
import re
import shutil
import sys
import yaml

from .db import DB

from .__metadata__ import __version__

logging.basicConfig(format="%(asctime)s: %(message)s",
                    level=logging.INFO)

_CONFIG = {
    "api_id": os.getenv("API_ID", ""),
    "api_hash": os.getenv("API_HASH", ""),
    "group": "",
    "chats": [],
    "download_avatars": True,
    "avatar_size": [64, 64],
    "download_media": False,
    "media_dir": "media",
    "media_mime_types": [],
    "proxy": {
        "enable": False,
    },
    "fetch_batch_size": 2000,
    "fetch_wait": 5,
    "fetch_limit": 0,

    # Fetch the profile bio of every synced user (one extra API call
    # per user, cached in the DB).
    "fetch_user_bios": False,

    "publish_rss_feed": True,
    "rss_feed_entries": 100,

    "publish_dir": "site",
    "site_url": "https://mysite.com",
    "static_dir": "static",
    "telegram_url": "https://t.me/{id}",
    "per_page": 1000,
    "show_sender_fullname": False,
    "timezone": "",
    "site_name": "@{group} (Telegram) archive",
    "site_description": "Public archive of @{group} Telegram messages.",
    "meta_description": "@{group} {date} Telegram message archive.",
    "page_title": "{date} - @{group} Telegram message archive."
}


def get_config(path):
    config = {}
    with open(path, "r") as f:
        config = {**_CONFIG, **yaml.safe_load(f.read())}
    return config


# Public chat URLs on t.me / telegram.me with an optional scheme,
# an optional trailing slash, and an optional query string / fragment.
_TME_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?P<name>[A-Za-z0-9_]+)/?(?:[?#].*)?$",
    re.IGNORECASE)

# Invite URLs (https://t.me/+abc123) cannot be resolved to a chat.
_TME_INVITE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/\+", re.IGNORECASE)


def parse_chat_ref(ref):
    """
    Normalize a single chat reference into an int chat ID, an @username,
    or a display name.

    Accepts chat IDs (-100123), t.me URLs (https://t.me/group -> @group),
    @usernames, and bare usernames / display names. Raises ValueError on
    empty references and invite links, which cannot be resolved to a chat.
    """
    if isinstance(ref, int):
        return ref

    ref = str(ref).strip()
    if not ref:
        raise ValueError("empty chat reference")

    try:
        return int(ref)
    except ValueError:
        pass

    if _TME_INVITE_RE.match(ref):
        raise ValueError(
            "invite links cannot be resolved to a chat: {}".format(ref))

    m = _TME_URL_RE.match(ref)
    if m:
        return "@" + m.group("name")

    return ref


def parse_chats(s):
    """Parse a comma-separated chats string into a list of chat references.

    Each entry may be a chat ID, a t.me URL, an @username, or a display
    name. URLs are normalized to @usernames.
    """
    if not s:
        return []
    chats = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        chats.append(parse_chat_ref(part))
    return chats


def main():
    """Run the CLI."""
    p = argparse.ArgumentParser(
        description="A tool for exporting and archiving Telegram groups to webpages.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    p.add_argument("-c", "--config", action="store", type=str, default="config.yaml",
                   dest="config", help="path to the config file")
    p.add_argument("-d", "--data", action="store", type=str, default="data.sqlite",
                   dest="data", help="path to the SQLite data file to store messages")
    p.add_argument("-se", "--session", action="store", type=str, default="session.session",
                   dest="session", help="path to the session file")
    p.add_argument("-v", "--version", action="store_true", dest="version", help="display version")

    n = p.add_argument_group("new")
    n.add_argument("-n", "--new", action="store_true",
                   dest="new", help="initialize a new site")
    n.add_argument("-p", "--path", action="store", type=str, default="example",
                   dest="path", help="path to create the site")

    s = p.add_argument_group("sync")
    s.add_argument("-s", "--sync", action="store_true",
                   dest="sync", help="sync data from telegram group to the local DB")
    s.add_argument("-id", "--id", action="store", type=int, nargs="+",
                   dest="id", help="sync (or update) messages for given ids")
    s.add_argument("-from-id", "--from-id", action="store", type=int,
                   dest="from_id", help="sync (or update) messages from this id to the latest")
    s.add_argument("--chats", action="store", type=str, default=None,
                   dest="chats", help="comma-separated list of chats to sync. Accepts chat IDs (-100123), t.me URLs (https://t.me/group), usernames (@group) and display names (overrides the config file)")
    s.add_argument("--list-chats", action="store_true", dest="list_chats",
                   help="list all available dialogs with IDs, usernames and titles, then exit")
    s.add_argument("--filter", action="store", type=str, default=None,
                   dest="chat_filter", help="regex to match against chat titles. Only matching chats are synced. With no explicit chats, matches against all dialogs")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="resolve the chats and show which ones would be synced, without syncing")

    b = p.add_argument_group("build")
    b.add_argument("-b", "--build", action="store_true",
                   dest="build", help="build the static site")
    b.add_argument("-t", "--template", action="store", type=str, default="template.html",
                   dest="template", help="path to the template file")
    b.add_argument("--rss-template", action="store", type=str, default=None,
                   dest="rss_template", help="path to the rss template file")
    b.add_argument("--symlink", action="store_true", dest="symlink",
                   help="symlink media and other static files instead of copying")

    args = p.parse_args(args=None if sys.argv[1:] else ['--help'])

    if args.version:
        print("v{}".format(__version__))
        sys.exit()

    # Setup new site.
    elif args.new:
        exdir = os.path.join(os.path.dirname(__file__), "example")
        if not os.path.isdir(exdir):
            logging.error("unable to find bundled example directory")
            sys.exit(1)

        try:
            shutil.copytree(exdir, args.path)
        except FileExistsError:
            logging.error(
                "the directory '{}' already exists".format(args.path))
            sys.exit(1)
        except:
            raise

        logging.info("created directory '{}'".format(args.path))
        
        # make sure the files are writable
        os.chmod(args.path, 0o755)
        for root, dirs, files in os.walk(args.path):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)

    # List the chats (dialogs) available to the authorized user.
    elif args.list_chats:
        # Import because the Telegram client import is quite heavy.
        from .sync import Sync
        # patch for python 3.14
        import asyncio

        cfg = get_config(args.config)

        # Added the following 5 lines for compatibility with Python 3.14
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            s = Sync(cfg, args.session, None)
            s.list_chats()
        except KeyboardInterrupt as e:
            logging.info("cancelled manually")
            if cfg.get("use_takeout", False):
                s.finish_takeout()
            sys.exit()
        except:
            raise

    # Sync from Telegram.
    elif args.sync:
        # Import because the Telegram client import is quite heavy.
        from .sync import Sync
        # patch for python 3.14
        import asyncio  

        if args.id and args.from_id and args.from_id > 0:
            logging.error("pass either --id or --from-id but not both")
            sys.exit(1)

        cfg = get_config(args.config)
        # CLI --chats overrides the config file's chats / group.
        if args.chats:
            try:
                cfg["chats"] = parse_chats(args.chats)
            except ValueError as e:
                logging.error("invalid --chats value: {}".format(e))
                sys.exit(1)
        mode = "takeout" if cfg.get("use_takeout", False) else "standard"

        logging.info("starting Telegram sync (batch_size={}, limit={}, wait={}, mode={})".format(
            cfg["fetch_batch_size"], cfg["fetch_limit"], cfg["fetch_wait"], mode
        ))

       # Added the following 5 lines for compatibility with Python 3.14
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            s = Sync(cfg, args.session, DB(args.data))
            s.sync(args.id, args.from_id,
                   chat_filter=args.chat_filter, dry_run=args.dry_run)
        except KeyboardInterrupt as e:
            logging.info("sync cancelled manually")
            if cfg.get("use_takeout", False):
                s.finish_takeout()
            sys.exit()
        except:
            raise
    # Build static site.
    elif args.build:
        from .build import Build

        logging.info("building site")
        config = get_config(args.config)
        b = Build(config, DB(args.data, config["timezone"]), args.symlink)
        b.load_template(args.template)
        if args.rss_template:
            b.load_rss_template(args.rss_template)
        b.build()

        logging.info("published to directory '{}'".format(config["publish_dir"]))
