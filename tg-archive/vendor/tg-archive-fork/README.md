
![favicon](https://user-images.githubusercontent.com/547147/111869334-eb48f100-89a4-11eb-9c0c-bc74cdee197a.png)


**tg-archive** is a tool for exporting Telegram group chats into static websites, preserving chat history like mailing list archives.

**IMPORTANT:** I'm no longer actively maintaining or developing this tool. Can review and merge PRs (as long as they're not massive and are clearly documented).

## Preview
The [@fossunited](https://tg.fossunited.org) Telegram group archive.

![image](https://user-images.githubusercontent.com/547147/111869398-44188980-89a5-11eb-936f-01d98276ba6a.png)


## How it works
tg-archive uses the [Telethon](https://github.com/LonamiWebs/Telethon) Telegram API client to periodically sync messages from a group to a local SQLite database (file), downloading only new messages since the last sync. It then generates a static archive website of messages to be published anywhere.

## Features
- Periodically sync Telegram group messages to a local DB.
- Sync multiple chats into a single archive.
- Select chats flexibly: by ID, t.me URL, @username, display name, or regex filter.
- Track forum topics (threads): messages store their topic ID, searchable via a topic-scoped FTS5 index.
- Persist chat and topic metadata (titles, descriptions, forum flags) in the DB.
- Optionally enrich user profiles with their bios.
- Download user avatars locally.
- Download and embed media (files, documents, photos).
- Renders poll results.
- Use emoji alternatives in place of stickers.
- Single file Jinja HTML template for generating the static site.
- Year / Month / Day indexes with deep linking across pages.
- "In reply to" on replies with links to parent messages across pages.
- RSS / Atom feed of recent messages.

## Install
- Get [Telegram API credentials](https://my.telegram.org/auth?to=apps). Normal user account API and not the Bot API.
  - If this page produces an alert stating only "ERROR", disconnect from any proxy/vpn and try again in a different browser.

- Install with: `uv pip install tg-archive` (tested with Python 3.13.2).

### Usage

1. `tg-archive --new --path=mysite` (creates a new site. `cd` into mysite and edit `config.yaml`).
1. `tg-archive --sync` (syncs data into `data.sqlite`).
  Note: First time connection will prompt for your phone number + a Telegram auth code sent to the app. On successful auth, a `session.session` file is created. DO NOT SHARE this session file publicly as it contains the API autorization for your account.
1. `tg-archive --build` (builds the static site into the `site` directory, which can be published)

### Syncing multiple chats

By default tg-archive syncs the single chat configured with `group`. To sync several chats into the same archive, list them under `chats` in `config.yaml`:

```yaml
chats:
  - -1001234567890
  - -1009876543210
```

Numeric chat IDs as well as `@usernames` / group names work. Chats can also be passed per run, which overrides the config file:

```shell
tg-archive --sync --chats=-1001234567890,-1009876543210
```

Every message is stored with its `chat_id`, and each chat keeps its own sync offset, so re-running `--sync` only fetches new messages per chat. Single `group` setups keep working unchanged.

Note: databases created before multi-chat support are migrated automatically (a `chat_id` column is added). To sync multiple chats, start with a fresh `data.sqlite` so the composite message key is in place from the start.

### Selecting chats: URLs, names, and filters

Chat references — in the config file and in `--chats` — accept any of:

- Chat IDs: `-1001234567890` (the marked form) or the plain entity ID.
- Public chat URLs: `https://t.me/groupname` (normalized to `@groupname`). Invite links (`t.me/+...`) are not supported.
- Usernames: `@groupname`.
- Display names: `My Group` (exact, case-insensitive title match).

```shell
tg-archive --sync --chats="https://t.me/fossunited,@mychannel,My Group"
```

To see which chats your account can sync, list all dialogs with their IDs, usernames, and titles:

```shell
tg-archive --list-chats
```

To sync every chat whose title matches a regex (matched with `re.search`, case-sensitive):

```shell
tg-archive --sync --filter ".*engineering.*"
```

The filter also narrows down an explicit chat list — only the listed chats whose titles match are synced.

To check which chats a sync would touch without actually syncing:

```shell
tg-archive --sync --chats=@mychannel,https://t.me/other --dry-run
tg-archive --sync --filter ".*engineering.*" --dry-run
```

All references are resolved to entity IDs before syncing, so unresolvable chats are reported up front.

### Forum topics (threads)

For groups with topics enabled, every message is stored with the ID of the topic it belongs to, in the `thread_id` column:

- Messages inside a forum topic carry `thread_id` = the topic's root message ID (the topic ID). This includes the topic root message itself.
- Groups without topics (and messages outside any topic) carry `thread_id = NULL`.

The DB also maintains an FTS5 full-text index over message content (`messages_fts`) that carries `chat_id` and `thread_id`, so keyword searches can be scoped to a single chat or topic:

```sql
SELECT * FROM messages_fts
WHERE messages_fts MATCH 'search terms' AND thread_id = 42;
```

Note: databases created before thread support are migrated automatically (a `thread_id` column is added, and the FTS index is created and backfilled with the existing messages).

### Stored metadata

Besides messages, users, and media, the archive keeps chat- and topic-level metadata:

- `chats` — one row per synced chat with its title, username, description (from the Telegram `GetFullChannel`/`GetFullChat` request), type, forum flag, participant count, and last sync time.
- `topics` — one row per forum topic with its title, creator, creation date, and closed state. Titles are captured from topic creation / edit service messages, and backfilled for topics created before this feature.
- `users.bio` — profile descriptions, only filled when `fetch_user_bios` is enabled:

```yaml
fetch_user_bios: true
```

Bios cost one additional API call per user and are cached in the DB, so a repeat sync does not re-fetch them.

### Customization
Edit the generated `template.html` and static assets in the `./static` directory to customize the site.

### Note
- The sync can be stopped (Ctrl+C) any time to be resumed later.
- Setup a cron job to periodically sync messages and re-publish the archive.
- Downloading large media files and long message history from large groups continuously may run into Telegram API's rate limits. Watch the debug output.

Licensed under the MIT license.
