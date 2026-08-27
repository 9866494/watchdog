# Руководство по авторизации tg-archive

## Обзор

tg-archive использует MTProto (Telethon) для подключения к Telegram. Авторизация происходит один раз при первом запуске — создаётся `session.session` в Docker volume. Для нового аккаунта или чата нужно повторить процесс.

## Требования

- Отдельный аккаунт Telegram (не личный!)
- Docker и Docker Compose
- Orca (для интерактивного ввода SMS-кода)

## Шаг 1: Получить API credentials

1. Перейти на https://my.telegram.org/auth?to=apps
2. Войти с номера телефона **отдельного аккаунта** (не личного)
3. Создать приложение:
   - App title: любое (например "Watchdog")
   - Short name: 5-32 символа (латиница, цифры)
   - Description: любое
   - URL: можно оставить пустым
4. После создания получить:
   - **api_id** — число (например `12345678`)
   - **api_hash** — 32 hex символа (например `abcdef1234567890...`)

**Важно:** Если my.telegram.org выдаёт ошибку, отключить VPN/прокси и попробовать в другом браузере.

## Шаг 2: Узнать ID чата

tg-archive синхронизирует один чат. Нужен числовой ID (не username).

**Способы получить ID:**

1. **Через бота @userinfobot:**
   - Добавить бота в целевой чат
   - Бот покажет ID чата (начинается с `-` для групп, например `-1001234567890`)

2. **Через бота @getidsbot:**
   - Переслать любое сообщение из целевого чата боту
   - Бот покажет ID чата

3. **Через Telegram Web:**
   - Открыть чат в https://web.telegram.org
   - В URL будет ID (например `https://web.telegram.org/z/#-1001234567890`)

**Важно:** Для приватных каналов ID начинается с `-100`, для публичных групп — с `-`.

## Шаг 3: Заполнить .env

Создать `.env` в корне проекта (или скопировать из `.env.example`):

```bash
cp .env.example .env
```

Заполнить переменные:

```env
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890
TG_PHONE=+79991234567
TG_CHAT_IDS=-1001234567890
```

**TG_PHONE** — номер телефона аккаунта, от которого подключается tg-archive (в международном формате).

**TG_CHAT_IDS** — числовой ID чата (из шага 2).

**Безопасность:** `.env` уже в `.gitignore`, не коммитить.

## Шаг 4: Запустить авторизацию

Авторизация требует интерактивного ввода SMS-кода. Использовать Orca terminal:

```bash
# Создать терминал
orca terminal create --worktree active --title "tg-archive auth" --json

# Получить handle из вывода (например term_abc123...)

# Отправить команду авторизации
orca terminal send --terminal <handle> --text "docker compose run --rm tg-archive --new" --enter --json
```

**Что произойдёт:**
1. Контейнер создаст `config.yaml` из `.env`
2. Telethon подключится к Telegram
3. Запросит номер телефона (подставится из config.yaml автоматически)
4. Запросит SMS-код — **ввести вручную в терминале Orca**
5. При успехе создастся `session.session` в Docker volume

**Проверка:** После ввода кода должен появиться лог:
```
2026-XX-XX XX:XX:XX: created directory '<chat-name>'
```

## Шаг 5: Синхронизация

После успешной авторизации запустить синхронизацию:

```bash
orca terminal create --worktree active --title "tg-archive sync" --json
orca terminal send --terminal <handle> --text "docker compose run --rm tg-archive --sync" --enter --json
```

**Что произойдёт:**
1. Контейнер подключится к Telegram (используя session.session)
2. Скачает историю чата в SQLite (`data.sqlite` в Docker volume)
3. Логи покажут прогресс: `fetched 300 messages`, `fetched 600 messages`, ...

**Проверка:** После завершения:
```bash
# Проверить количество сообщений
docker run --rm -v telegram_data:/data python:3.11-slim bash -c "
  apt-get update -qq && apt-get install -y -qq sqlite3 > /dev/null 2>&1
  sqlite3 /data/data.sqlite 'SELECT COUNT(*) FROM messages;'
"
```

Должно вывести число > 0.

## Шаг 6: Запуск MCP-сервера

После синхронизации запустить MCP-сервер:

```bash
docker compose up -d watchdog-mcp
```

**Проверка:**
```bash
docker compose logs -f watchdog-mcp
```

Должны появиться логи:
```
[INFO] Запуск Watchdog MCP сервера...
[INFO] WAL mode включён
[INFO] FTS5 индекс создан и заполнен
[INFO] MCP-сервер запущен (stdio transport)
```

## Смена аккаунта

Чтобы подключить другой аккаунт Telegram:

1. **Удалить session.session:**
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/session.session
   ```

2. **Обновить .env:**
   ```bash
   # Заполнить новые credentials
   nano .env
   ```

3. **Повторить авторизацию** (шаг 4)

4. **Удалить старую синхронизацию** (опционально):
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/data.sqlite
   ```

5. **Повторить синхронизацию** (шаг 5)

## Смена чата

Чтобы синхронизировать другой чат (без смены аккаунта):

1. **Обновить .env:**
   ```bash
   # Изменить TG_CHAT_IDS
   nano .env
   ```

2. **Удалить старый config.yaml:**
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/config.yaml
   ```

3. **Удалить старую синхронизацию** (опционально):
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/data.sqlite
   ```

4. **Повторить синхронизацию** (шаг 5)

**Важно:** session.session остаётся — авторизация не нужна.

## Troubleshooting

### Ошибка "PHONE_CODE_INVALID"

SMS-код введён неправильно или истёк. Повторить авторизацию (шаг 4).

### Ошибка "PHONE_NUMBER_INVALID"

Неверный формат номера в `.env`. Должен быть в международном формате: `+79991234567`.

### Ошибка "CHANNEL_PRIVATE"

Аккаунт не является участником целевого чата. Сначала вступить в чат через обычный Telegram-клиент, затем повторить синхронизацию.

### Ошибка "AUTH_KEY_UNREGISTERED"

Сессия истекла или была отозвана. Удалить session.session (см. "Смена аккаунта") и повторить авторизацию.

### Синхронизация зависает

Telegram может ограничивать скорость (FloodWait). tg-archive автоматически ждёт и повторяет. Если зависает надолго — прервать (Ctrl+C) и повторить.

### Пустые сообщения

Некоторые сообщения имеют пустой `content` (стикеры, сервисные сообщения type=user_joined). Это нормально — tg-archive сохраняет только текст.

## Периодическая синхронизация

Для автоматического обновления данных настроить cron:

```bash
# Добавить в crontab (каждый час)
0 * * * * cd /path/to/watchdog && docker compose run --rm tg-archive --sync >> /var/log/watchdog-sync.log 2>&1
```

Или использовать Docker restart policy (уже настроено в docker-compose.yml).

## Подключение к MCP-клиенту

После запуска MCP-сервер доступен через stdio. Для подключения к opencode:

```json
{
  "mcpServers": {
    "watchdog": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network", "watchdog_default", "watchdog-mcp"]
    }
  }
}
```

Доступные tools:
- `search_messages` — полнотекстовый поиск по сообщениям
- `get_user_profiles` — список участников чата
- `get_chat_stats` — статистика активности за день/неделю/месяц

## Дополнительные ресурсы

- [Design doc](./designs/watchdog-data-pipeline-mcp.md) — архитектура и схема БД
- [README](../README.md) — быстрый старт
- [tg-archive](https://github.com/knadh/tg-archive) — исходный код (unmaintained, но работает)
