# Watchdog

Мониторинг Telegram-чатов через MCP-сервер. Пассивно наблюдает за группой, собирает сообщения и профили участников, отдаёт данные через MCP tools для анализа.

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
│     opencode    │
│  (MCP Client)   │
└─────────────────┘
```

## Требования

- Docker и Docker Compose
- Telegram API credentials (api_id, api_hash)
- Отдельный аккаунт Telegram для юзербота (не личный!)
- ID целевого чата для мониторинга

## Быстрый старт

### 1. Получить Telegram API credentials

1. Перейти на https://my.telegram.org/auth?to=apps
2. Войти с номера телефона отдельного аккаунта
3. Создать приложение → получить `api_id` и `api_hash`

### 2. Узнать ID чата

- Добавить бота [@userinfobot](https://t.me/userinfobot) в целевой чат
- Или переслать любое сообщение из чата боту [@getidsbot](https://t.me/getidsbot)
- Скопировать числовой ID (начинается с `-` для групп)

### 3. Настроить .env

```bash
cp .env.example .env
```

Заполнить `.env`:
```
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890
TG_PHONE=+79991234567
TG_CHAT_IDS=-1001234567890
```

### 4. Первый запуск tg-archive (интерактивный)

```bash
docker compose run --rm tg-archive --sync
```

При первом запуске Telethon запросит номер телефона и SMS-код для авторизации. После успешного входа создастся `session.session` в Docker volume.

**Важно:** флаг `--new` авторизацию не выполняет — он копирует шаблон сайта. Авторизация всегда происходит при первом `--sync`.

### 5. Синхронизация сообщений

```bash
docker compose run --rm tg-archive --sync
```

Скачает историю чата в SQLite. Повторять периодически для обновления данных.

### 6. Запуск MCP-сервера

```bash
docker compose up -d watchdog-mcp
```

MCP-сервер запустится в фоне, инициализирует FTS5 индекс и будет готов принимать запросы.

### 7. Настройка opencode

Создать `opencode.json` в корне проекта:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "watchdog": {
      "type": "local",
      "command": [
        "docker", "run", "-i", "--rm",
        "-v", "telegram_data:/data",
        "-e", "WATCHDOG_DB_PATH=/data/data.sqlite",
        "watchdog-watchdog-mcp:latest"
      ],
      "enabled": true
    }
  }
}
```

Перезапустить opencode (конфиг читается только при старте). Теперь доступны инструменты:
- `search_messages` — полнотекстовый поиск по сообщениям
- `get_user_profiles` — список участников чата
- `get_chat_stats` — статистика активности за день/неделю/месяц
- и ещё 12 tools (topics, activity, media, управление чатами и синхронизацией)

## Структура проекта

```
watchdog/
├── .env                    # Секреты (не в git)
├── .env.example            # Шаблон .env
├── docker-compose.yml      # Оркестрация контейнеров
├── tg-archive/
│   ├── Dockerfile          # Образ для tg-archive
│   └── entrypoint.sh       # Генерация config.yaml из env
└── watchdog-mcp/
    ├── Dockerfile          # Образ для MCP-сервера
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
# Первый запуск (интерактивная авторизация + синхронизация)
docker compose run --rm tg-archive --sync

# Синхронизация (скачать новые сообщения)
docker compose run --rm tg-archive --sync

# Запуск MCP-сервера
docker compose up -d watchdog-mcp

# Остановка MCP-сервера
docker compose down

# Логи MCP-сервера
docker compose logs -f watchdog-mcp

# Проверка статуса
docker compose ps
```

## Разработка

MCP-сервер написан на Python с использованием официального SDK `mcp[cli]`. Логирование идёт в stderr (не stdout), чтобы не ломать JSON-RPC через stdio.

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
