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

MCP-сервер — stdio-процесс, клиент спавнит его сам. Универсальная команда:

```
docker run -i --rm -v telegram_data:/data -e WATCHDOG_DB_PATH=/data/data.sqlite watchdog-mcp:latest
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
