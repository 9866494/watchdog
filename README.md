# Watchdog

Мониторинг Telegram-чатов через MCP-сервер. Пассивно наблюдает за группами,
собирает сообщения и профили участников, отдаёт данные через MCP tools для анализа.

## Архитектура

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Telegram Chat  │─────▶│   tg-archive     │─────▶│   SQLite DB     │
│   (MTProto)     │      │  (Python/Telethon)│      │  (data.sqlite)  │
└─────────────────┘      └──────────────────┘      └────────┬────────┘
                                                             │
                                                             ▼
                                                     ┌─────────────────┐
                                                     │  watchdog-mcp   │
                                                     │  (Python MCP)   │
                                                     └────────┬────────┘
                                                              │
                                                              ▼
┌─────────────────┐
│   MCP-клиент    │   opencode, openclaw, любой stdio-клиент
└─────────────────┘
```

## Требования

- git, Docker, Docker Compose v2
- Telegram API credentials (api_id, api_hash) с https://my.telegram.org
- Отдельный аккаунт Telegram для юзербота (не личный!)
- PTY-терминал для первой авторизации (SMS-код вводится интерактивно)

## Быстрый старт (одна команда на каждый шаг)

```bash
git clone https://github.com/9866494/watchdog.git && cd watchdog
cp .env.example .env          # заполнить TG_API_ID, TG_API_HASH, TG_PHONE, TG_CHAT_IDS
./scripts/bootstrap.sh
```

`bootstrap.sh` сам проверит окружение, соберёт образы, проведёт авторизацию
(спросит телефон и SMS-код в терминале), синхронизирует чаты и проверит,
что MCP-сервер отвечает. Повторный запуск делает только недостающие шаги.

Не знаешь ID чата? Получи список после авторизации:

```bash
./scripts/bootstrap.sh --chats   # id=... title=... по всем диалогам аккаунта
```

Впиши нужный ID в `TG_CHAT_IDS` в `.env`, удали stale-конфиг и досинхронизируйся:

```bash
docker run --rm -v telegram_data:/data alpine rm -f /data/config.yaml
./scripts/bootstrap.sh --sync
```

### Режимы bootstrap

```bash
./scripts/bootstrap.sh --check   # проверки: docker, .env, сессия, БД
./scripts/bootstrap.sh --build   # пересборка образов
./scripts/bootstrap.sh --sync    # инкрементальная синхронизация
./scripts/bootstrap.sh --chats   # список чатов аккаунта
./scripts/bootstrap.sh --verify  # stdio handshake MCP-сервера
```

## Получение Telegram API credentials

1. https://my.telegram.org/auth?to=apps — войти с номера **отдельного** аккаунта
2. Создать приложение: App title — любое, Short name — 5–32 символа латиницей
3. Скопировать `api_id` (число) и `api_hash` (32 hex-символа) в `.env`

Если my.telegram.org выдаёт ошибку — отключить VPN/прокси и попробовать другой браузер.

## Подключение MCP-клиента

MCP-сервер — stdio-процесс, клиент спавнит его сам. Универсальная команда
(монтирование docker.sock обязательно: tools `authorize`, `run_sync`,
`list_available_chats` управляют контейнером tg-archive через Docker API):

```
docker run -i --rm -v telegram_data:/data -v /var/run/docker.sock:/var/run/docker.sock -e WATCHDOG_DB_PATH=/data/data.sqlite watchdog-mcp:latest
```

### opencode

Готовый конфиг уже в репозитории (`opencode.json`). Для другого проекта — скопировать
блок `mcp` из него. Перезапустить opencode после изменения конфига.

### Claude Code и другие клиенты с JSON-конфигом

```json
{
  "mcpServers": {
    "watchdog": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "telegram_data:/data",
               "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-e", "WATCHDOG_DB_PATH=/data/data.sqlite", "watchdog-mcp:latest"]
    }
  }
}
```

### Tools MCP-сервера

- `search_messages` — полнотекстовый поиск по сообщениям
- `get_chat_stats` — статистика активности за день/неделю/месяц
- `get_chat_topics` — топики (треды) чата
- `get_user_profiles`, `get_user_activity` — участники и их активность
- `get_recent_messages`, `get_message_by_id` — чтение сообщений
- `get_media`, `get_chats`, `get_chat_activity`
- `list_available_chats`, `add_chat`, `remove_chat`, `run_sync`, `authorize` — управление синхронизацией и авторизацией через контейнер tg-archive

## Структура проекта

```
watchdog/
├── .env                    # Секреты (не в git)
├── .env.example            # Шаблон .env
├── opencode.json           # MCP-конфиг для opencode
├── docker-compose.yml      # Оркестрация контейнеров (стабильные имена образов)
├── scripts/
│   └── bootstrap.sh        # Автоматический сетап: проверки → build → auth → sync → verify
├── docs/
│   └── auth-guide.md       # Подробное руководство по авторизации и смене аккаунта/чата
├── tg-archive/
│   ├── Dockerfile          # Образ tg-archive (форк вендорен в vendor/)
│   ├── vendor/tg-archive-fork  # Исходники форка (в git)
│   └── entrypoint.sh       # Генерация config.yaml из env
└── watchdog-mcp/
    ├── Dockerfile          # Образ MCP-сервера
    ├── requirements.txt    # Python зависимости
    └── server.py           # MCP-сервер с tools
```

## Схема БД (из tg-archive)

```sql
messages: id, type, date, edit_date, content, reply_to, user_id, media_id
users: id, username, first_name, last_name, tags, avatar
media: id, type, url, title, description, thumb
```

FTS5 индекс создаётся автоматически при первом запуске MCP-сервера.

## Команды

```bash
# Полный сетап с нуля (авторизация + синхронизация + проверка)
./scripts/bootstrap.sh

# Инкрементальная синхронизация
./scripts/bootstrap.sh --sync

# Проверка статуса
./scripts/bootstrap.sh --check

# Остановка и очистка
docker compose --profile sync --profile mcp down

# Логи последнего прогона синхронизации — в выводе команды --sync
```

Синхронизация хранит данные в Docker volume `telegram_data` — переживает
пересоздание контейнеров и обновление репозитория.

## Автономный сетап агентом (чат-режим)

Сценарий: агент работает с машиной через канал (Telegram, Discord), человек
дает только данные в чат. Агент выполняет всё сам, PTY не нужен.

Агент запрашивает у человека в чат: `api_id`, `api_hash`, номер телефона,
SMS-код (придёт после первого шага авторизации), пароль 2FA (если включён).

### Шаг 1. Развёртывание

```bash
git clone https://github.com/9866494/watchdog.git && cd watchdog
cat > .env <<EOF
TG_API_ID=<из чата>
TG_API_HASH=<из чата>
TG_PHONE=<из чата>
TG_CHAT_IDS=<из чата или позднее через --chats>
EOF
docker compose build tg-archive watchdog-mcp
```

### Шаг 2. config.yaml до авторизации

Entrypoint генерирует `config.yaml` из `.env` при любом запуске контейнера.
`--new` копирует шаблон сайта и выходит — неинтерактивно:

```bash
docker compose run --rm tg-archive --new
```

### Шаг 3. Подключить watchdog MCP к агенту

См. раздел «Подключение MCP-клиента». Для opencode конфиг уже в `opencode.json`.
Монтирование `/var/run/docker.sock` обязательно — через него работают tools
`authorize` и `run_sync`.

### Шаг 4. Авторизация через MCP tools

Человек вводит SMS-код прямо в чат — агент прокидывает его в Telegram:

```
authorize(phone="+79991234567")          → status: code_sent
# человек прислал код в чат
authorize(phone, code="12345")           → status: authorized
```

При 2FA: `authorize(phone, code)` вернёт `password_needed`, агент запросит
пароль в чат и повторит `authorize(phone, code, password)`.

После `authorized` сессия сохранена, синхронизация идёт в фоне. Дальше:
`list_available_chats` — узнать ID чатов, `add_chat(chat_ref)` — добавить чат
в мониторинг, `run_sync` — обновить данные.

### Шаг 5. Проверка

```bash
./scripts/bootstrap.sh --check   # сессия, база
./scripts/bootstrap.sh --verify  # MCP-сервер отвечает
```

### Fallback без MCP-клиента

Если MCP-клиент ещё не подключен, авторизация возможна пайпом stdin
(код запрашивать в чат заранее; при 2FA путь не работает — используйте MCP):

```bash
docker compose run -T --rm tg-archive --sync <<EOF
+79991234567
12345
EOF
```

## Разработка

MCP-сервер написан на Python с использованием SDK `mcp[cli]`. Логирование идёт
в stderr (не stdout), чтобы не ломать JSON-RPC через stdio.

Для локальной разработки без Docker:

```bash
cd watchdog-mcp
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export WATCHDOG_DB_PATH=/path/to/data.sqlite
python server.py
```

## Лицензия

MIT
