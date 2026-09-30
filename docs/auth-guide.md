# Руководство по авторизации tg-archive

## Обзор

tg-archive использует MTProto (Telethon) для подключения к Telegram. Авторизация
происходит один раз при первом `--sync` — создаётся `session.session` в Docker
volume `telegram_data`. Для нового аккаунта или чата нужно повторить процесс.

## Требования

- Отдельный аккаунт Telegram (не личный!)
- Docker и Docker Compose v2
- Любой PTY-терминал для интерактивного ввода SMS-кода

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

ID чата можно узнать до и после авторизации.

**После авторизации (рекомендуется):**

```bash
./scripts/bootstrap.sh --chats
```

Выведет все диалоги аккаунта: `id=... username=... title=...`. Для групп и
каналов id начинается с `-100`.

**До авторизации (вручную):**

1. **Через бота @userinfobot:** добавить бота в целевой чат — покажет ID чата
2. **Через бота @getidsbot:** переслать любое сообщение из чата боту
3. **Через Telegram Web:** открыть чат в web.telegram.org — ID будет в URL

## Шаг 3: Заполнить .env

```bash
cp .env.example .env
```

```env
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890
TG_PHONE=+79991234567
TG_CHAT_IDS=-1001234567890
```

**TG_PHONE** — номер телефона аккаунта (международный формат).
**TG_CHAT_IDS** — числовой ID чата (или список через запятую).

**Безопасность:** `.env` уже в `.gitignore`, не коммитить.

## Шаг 4: Авторизация

Первый запуск — интерактивный. Выполнить в PTY-терминале:

```bash
./scripts/bootstrap.sh
```

Скрипт проверит окружение, соберёт образы и запустит:

```
docker compose run --rm tg-archive --sync
```

**Что произойдёт:**
1. Контейнер создаст `config.yaml` из `.env`
2. Telethon подключится к Telegram
3. Запросит номер телефона (подставить из `.env`)
4. Запросит SMS-код — ввести в терминале
5. При 2FA запросит пароль
6. При успехе создастся `session.session` в volume

**Важно:** флаг `--new` авторизацию не выполняет — он копирует bundled-шаблон
сайта в `/data/example`. Реальная авторизация происходит при первом `--sync`:
Telethon (`client.start()`) сам запрашивает телефон и код в интерактивном режиме.

**Проверка:**
```bash
./scripts/bootstrap.sh --check
```

Должно вывести `[ok] сессия есть`.

## Шаг 5: Синхронизация

Входит в полный `./scripts/bootstrap.sh`. Отдельно (инкрементально):

```bash
./scripts/bootstrap.sh --sync
```

Логи покажут прогресс: `fetched 300 messages`, `fetched 600 messages`, ...

**Проверка:**
```bash
docker run --rm -v telegram_data:/data alpine sh -c \
  "apk add -q sqlite >/dev/null && sqlite3 /data/data.sqlite 'SELECT COUNT(*) FROM messages;'"
```

Должно вывести число > 0.

## Шаг 6: Подключение MCP-клиента

См. README.md, раздел «Подключение MCP-клиента». Быстрая проверка сервера:

```bash
./scripts/bootstrap.sh --verify
```

## Смена аккаунта

1. **Удалить session.session:**
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/session.session
   ```

2. **Обновить .env** (новые api_id/api_hash и телефон)

3. **Повторить авторизацию** (шаг 4)

4. **Удалить старую синхронизацию** (опционально):
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/data.sqlite
   ```

## Смена чата

1. **Обновить TG_CHAT_IDS в .env**

2. **Удалить старый config.yaml:**
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/config.yaml
   ```

3. **Удалить старую синхронизацию** (опционально):
   ```bash
   docker run --rm -v telegram_data:/data alpine rm /data/data.sqlite
   ```

4. **`./scripts/bootstrap.sh --sync`**

**Важно:** session.session остаётся — авторизация не нужна.

## Troubleshooting

### Ошибка "PHONE_CODE_INVALID"

SMS-код введён неправильно или истёк. Повторить авторизацию (шаг 4).

### Ошибка "PHONE_NUMBER_INVALID"

Неверный формат номера в `.env`. Должен быть в международном формате: `+79991234567`.

### Ошибка "CHANNEL_PRIVATE"

Аккаунт не является участником целевого чата. Сначала вступить в чат через
обычный Telegram-клиент, затем повторить синхронизацию. Список доступных чатов:
`./scripts/bootstrap.sh --chats`.

### Ошибка "AUTH_KEY_UNREGISTERED"

Сессия истекла или была отозвана. Удалить session.session (см. "Смена аккаунта") и повторить авторизацию.

### Синхронизация зависает

Telegram может ограничивать скорость (FloodWait). tg-archive автоматически ждёт
и повторяет. Если зависает надолго — прервать (Ctrl+C) и повторить.

### Пустые сообщения

Некоторые сообщения имеют пустой `content` (стикеры, сервисные сообщения
type=user_joined). Это нормально — tg-archive сохраняет только текст.

## Периодическая синхронизация

Для автоматического обновления данных настроить cron:

```bash
# Каждый час
0 * * * * cd /path/to/watchdog && ./scripts/bootstrap.sh --sync >> /var/log/watchdog-sync.log 2>&1
```

## Дополнительные ресурсы

- [README](../README.md) — быстрый старт и подключение MCP-клиента
- [tg-archive](https://github.com/knadh/tg-archive) — исходный код оригинала (unmaintained, но работает); в проекте используется вендоренный форк с поддержкой нескольких чатов и топиков
