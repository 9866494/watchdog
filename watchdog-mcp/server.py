"""
Watchdog MCP Server

MCP-сервер для мониторинга Telegram-чатов через данные tg-archive.
Читает SQLite БД и отдаёт MCP tools для поиска и анализа.

Схема БД (из форка tg-archive с поддержкой нескольких чатов и топиков):
- messages: id, chat_id, thread_id, type, date, edit_date, content, reply_to, user_id, media_id
  (PRIMARY KEY (id, chat_id); thread_id — топик форума, если чат с топиками)
- messages_fts: FTS5(content, chat_id UNINDEXED, thread_id UNINDEXED), rowid = messages.rowid
- users: id, username, first_name, last_name, tags, avatar
- media: id, type, url, title, description, thumb
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Логирование в stderr (не stdout!) для stdio транспорта
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Путь к SQLite БД (монтируется из tg-archive)
DB_PATH = os.environ.get("WATCHDOG_DB_PATH", "/data/data.sqlite")

# Инициализация MCP-сервера
mcp = MCPServer("watchdog")


def normalize_chat_id(chat_id: int) -> int:
    """
    Нормализовать chat_id: убрать маркер -100 (Telegram supergroup prefix).
    В БД chat_id хранится как 3429914743, а в Telegram API виден как -1003429914743.
    Принимает оба варианта.
    """
    if chat_id < -1000000000000:
        # -100-префикс: -1003429914743 -> 3429914743
        return -chat_id - 1000000000000
    return chat_id


def get_db() -> sqlite3.Connection:
    """Получить соединение с БД."""
    if not Path(DB_PATH).exists():
        raise ToolError(
            f"База данных не найдена: {DB_PATH}. "
            "Запустите синхронизацию: docker compose run --rm tg-archive --new"
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- Конфигурация и управление синхронизацией ---

# Путь к config.yaml и session (в том же volume, что и БД)
CONFIG_PATH = os.environ.get("WATCHDOG_CONFIG_PATH", "/data/config.yaml")
SESSION_PATH = os.environ.get("WATCHDOG_SESSION_PATH", "/data/session.session")


def load_config() -> dict:
    """Прочитать config.yaml tg-archive."""
    import yaml

    if not Path(CONFIG_PATH).exists():
        raise ToolError(
            f"config.yaml не найден: {CONFIG_PATH}. "
            "Сначала запустите: docker compose run --rm tg-archive --new"
        )
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg or {}


def save_config(cfg: dict):
    """Записать config.yaml."""
    import yaml

    # Сохранить в том же формате, что генерирует entrypoint.sh
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def get_chats_from_config() -> list:
    """Список чатов из config.yaml (chats или legacy group)."""
    cfg = load_config()
    chats = cfg.get("chats")
    if chats is None:
        group = cfg.get("group")
        return [group] if group else []
    if isinstance(chats, (str, int)):
        return [chats]
    return list(chats)


def set_chats_in_config(chats: list):
    """Обновить список chats в config.yaml."""
    cfg = load_config()
    if not chats:
        # Пустой список — переходим на legacy group (пустую)
        cfg.pop("chats", None)
        cfg.setdefault("group", "")
    else:
        cfg["chats"] = chats
        cfg.pop("group", None)
    save_config(cfg)


def normalize_chat_ref(chat_ref: str) -> str:
    """
    Нормализовать ссылку на чат в форму для config.yaml.
    Принимает: ID (-100123), t.me URL, @username, display name.
    Возвращает: число-строку для ID, @username для URL/username, иначе display name.
    """
    ref = chat_ref.strip()

    # t.me URL -> @username
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "telegram.me/"):
        if ref.startswith(prefix):
            username = ref[len(prefix):].split("/")[0].split("?")[0]
            if username.startswith("+"):
                raise ToolError(f"Инвайт-ссылки не поддерживаются: {chat_ref}")
            return f"@{username}" if not username.startswith("@") else username

    # Числовой ID
    try:
        int(ref)
        return ref
    except ValueError:
        pass

    # @username
    if ref.startswith("@"):
        return ref

    # Display name — оставляем как есть
    return ref


def run_tg_archive_sync(*args: str) -> str:
    """
    Запустить tg-archive контейнер через docker SDK и дождаться завершения.
    Возвращает полный лог (stdout+stderr).
    """
    try:
        import docker
    except ImportError:
        raise ToolError("docker SDK не установлен в образе watchdog-mcp")

    client = docker.from_env()
    try:
        container = client.containers.run(
            image="watchdog-tg-archive:latest",
            command=list(args),
            volumes={"telegram_data": {"bind": "/data", "mode": "rw"}},
            detach=True,
        )
    except docker.errors.ImageNotFound:
        raise ToolError(
            "Образ watchdog-tg-archive не найден. Соберите: docker compose build tg-archive"
        )

    try:
        result = container.wait()
        log = container.logs().decode("utf-8", errors="replace")
        status = result.get("StatusCode", 1)
        if status != 0:
            raise ToolError(
                f"tg-archive {' '.join(args)} завершился с ошибкой (exit {status}). Лог:\n{log[-2000:]}"
            )
        return log[-8000:]
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def init_db():
    """
    Инициализация БД: WAL mode, FTS5 индекс, триггеры.
    Вызывается при старте сервера.
    """
    if not Path(DB_PATH).exists():
        logger.warning(f"База данных не найдена: {DB_PATH}")
        logger.info("Ожидание первой синхронизации tg-archive...")
        return

    conn = sqlite3.connect(DB_PATH)

    # WAL mode для конкурентного чтения во время записи
    conn.execute("PRAGMA journal_mode=WAL")
    logger.info("WAL mode включён")

    # Миграция со старой схемы (один чат): колонки chat_id/thread_id
    cursor = conn.execute("PRAGMA table_info(messages)")
    cols = [row[1] for row in cursor.fetchall()]
    if "chat_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")
        logger.info("Добавлена колонка messages.chat_id")
    if "thread_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thread_id INTEGER")
        logger.info("Добавлена колонка messages.thread_id")

    # Проверка существования FTS5 индекса
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    )
    fts_exists = cursor.fetchone() is not None

    if not fts_exists:
        logger.info("Создаю FTS5 индекс (схема форка: content + UNINDEXED chat_id/thread_id)...")

        # FTS5 индекс по content с UNINDEXED-колонками для фильтрации по чату/топику
        conn.execute("""
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                chat_id UNINDEXED,
                thread_id UNINDEXED
            )
        """)

        # Заполнение индекса из существующих сообщений
        conn.execute("""
            INSERT INTO messages_fts (rowid, content, chat_id, thread_id)
            SELECT rowid, content, chat_id, thread_id FROM messages
        """)

        # Триггеры синхронизации с messages (совпадают с триггерами форка)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, chat_id, thread_id)
                VALUES (new.rowid, new.content, new.chat_id, new.thread_id);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, chat_id, thread_id)
                VALUES ('delete', old.rowid, old.content, old.chat_id, old.thread_id);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, chat_id, thread_id)
                VALUES ('delete', old.rowid, old.content, old.chat_id, old.thread_id);
                INSERT INTO messages_fts(rowid, content, chat_id, thread_id)
                VALUES (new.rowid, new.content, new.chat_id, new.thread_id);
            END
        """)

        conn.commit()
        logger.info("FTS5 индекс создан и заполнен")
    else:
        logger.info("FTS5 индекс уже существует")

    # Индекс по date для агрегаций
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)
    """)
    conn.commit()

    conn.close()
    logger.info("Инициализация БД завершена")


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """
    Парсить ISO 8601 дату в формат tg-archive (YYYY-MM-DD HH:MM:SS).

    Поддерживает:
    - "2024-01-01" → "2024-01-01 00:00:00"
    - "2024-01-01T10:00:00" → "2024-01-01 10:00:00"
    - "2024-01-01T10:00:00Z" → "2024-01-01 10:00:00"
    """
    if not date_str:
        return None

    # Убираю часовой пояс (tg-archive хранит без TZ)
    date_str = date_str.rstrip("Z")

    try:
        # Пробую полный формат
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    try:
        # Пробую только дату
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    raise ValueError(f"Неверный формат даты: {date_str}")


@mcp.tool()
async def search_messages(
    query: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    chat_id: Optional[int] = None,
    thread_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Поиск сообщений по полнотекстовому запросу (FTS5).

    Args:
        query: Поисковый запрос (поддерживает FTS5 синтаксис: "точное совпадение", OR, NOT)
        date_from: Начальная дата (ISO 8601: "2024-01-01" или "2024-01-01T10:00:00")
        date_to: Конечная дата (ISO 8601)
        chat_id: Фильтр по chat_id (числовой ID чата, например -1001234567890)
        thread_id: Фильтр по thread_id (ID топика форума внутри чата)
        limit: Максимальное количество результатов (по умолчанию 50)

    Returns:
        Список сообщений: id, chat_id, thread_id, content, user_id, date, reply_to
    """
    if chat_id is not None:
        chat_id = normalize_chat_id(chat_id)
    conn = get_db()

    # Парсинг дат
    try:
        date_from_parsed = parse_date(date_from)
        date_to_parsed = parse_date(date_to)
    except ValueError as e:
        raise ToolError(str(e)) from e

    # Построение запроса.
    # ВАЖНО: join по m.rowid = fts.rowid — FTS-индекс форка хранит rowid
    # таблицы messages, а id входит в составной ключ (id, chat_id).
    sql = """
        SELECT m.id, m.chat_id, m.thread_id, m.content, m.user_id, m.date, m.reply_to,
               u.username, u.first_name, u.last_name
        FROM messages_fts fts
        JOIN messages m ON m.rowid = fts.rowid
        LEFT JOIN users u ON u.id = m.user_id
        WHERE messages_fts MATCH ?
    """
    params = [query]

    if chat_id is not None:
        sql += " AND m.chat_id = ?"
        params.append(chat_id)

    if thread_id is not None:
        sql += " AND m.thread_id = ?"
        params.append(thread_id)

    if date_from_parsed:
        sql += " AND m.date >= ?"
        params.append(date_from_parsed)

    if date_to_parsed:
        sql += " AND m.date <= ?"
        params.append(date_to_parsed)

    sql += " ORDER BY m.date DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "chat_id": row["chat_id"],
            "thread_id": row["thread_id"],
            "content": row["content"],
            "user_id": row["user_id"],
            "date": row["date"],
            "reply_to": row["reply_to"],
            "author": {
                "username": row["username"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
            },
        })

    conn.close()
    return results


@mcp.tool()
async def get_user_profiles(limit: int = 100) -> list[dict]:
    """
    Получить профили пользователей.

    Args:
        limit: Максимальное количество пользователей (по умолчанию 100)

    Returns:
        Список пользователей: id, username, first_name, last_name, tags
    """
    conn = get_db()

    cursor = conn.execute("""
        SELECT id, username, first_name, last_name, tags
        FROM users
        ORDER BY id
        LIMIT ?
    """, [limit])

    results = []
    for row in cursor.fetchall():
        results.append({
            "id": row["id"],
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "tags": row["tags"],
        })

    conn.close()
    return results


@mcp.tool()
async def get_chat_stats(period: str) -> dict:
    """
    Получить статистику чата за период.

    Args:
        period: Период: "day" (сегодня), "week" (последние 7 дней), "month" (последние 30 дней)

    Returns:
        Статистика: message_count, active_users, top_authors (топ-10),
        chats (разбивка по chat_id: сообщения, авторы, топики)
    """
    conn = get_db()

    # Определение диапазона дат
    now = datetime.utcnow()
    if period == "day":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from = date_from - timedelta(days=6)
    elif period == "month":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from = date_from - timedelta(days=29)
    else:
        raise ToolError(f"Неверный период: {period}. Ожидается: day, week, month")

    date_from_str = date_from.strftime("%Y-%m-%d %H:%M:%S")

    # Общее количество сообщений
    cursor = conn.execute("""
        SELECT COUNT(*) as count
        FROM messages
        WHERE date >= ?
    """, [date_from_str])
    message_count = cursor.fetchone()["count"]

    # Количество активных пользователей
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT user_id) as count
        FROM messages
        WHERE date >= ?
    """, [date_from_str])
    active_users = cursor.fetchone()["count"]

    # Топ-10 авторов
    cursor = conn.execute("""
        SELECT u.username, u.first_name, u.last_name, COUNT(*) as message_count
        FROM messages m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE m.date >= ?
        GROUP BY m.user_id
        ORDER BY message_count DESC
        LIMIT 10
    """, [date_from_str])

    top_authors = []
    for row in cursor.fetchall():
        top_authors.append({
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "message_count": row["message_count"],
        })

    # Разбивка по чатам и топикам за период
    cursor = conn.execute("""
        SELECT chat_id,
               COUNT(*) as message_count,
               COUNT(DISTINCT user_id) as active_users,
               COUNT(DISTINCT thread_id) as topic_count
        FROM messages
        WHERE date >= ?
        GROUP BY chat_id
        ORDER BY message_count DESC
    """, [date_from_str])

    chats = []
    for row in cursor.fetchall():
        chats.append({
            "chat_id": row["chat_id"],
            "message_count": row["message_count"],
            "active_users": row["active_users"],
            "topic_count": row["topic_count"],
        })

    conn.close()

    return {
        "period": period,
        "date_from": date_from_str,
        "message_count": message_count,
        "active_users": active_users,
        "top_authors": top_authors,
        "chats": chats,
    }


@mcp.tool()
async def get_chats(limit: int = 100) -> list[dict]:
    """
    Список всех синхронизируемых чатов с метриками.

    Returns:
        По каждому чату: chat_id, message_count, active_users, topic_count,
        first_date, last_date
    """
    conn = get_db()
    cursor = conn.execute("""
        SELECT chat_id,
               COUNT(*) as message_count,
               COUNT(DISTINCT user_id) as active_users,
               COUNT(DISTINCT thread_id) as topic_count,
               MIN(date) as first_date,
               MAX(date) as last_date
        FROM messages
        GROUP BY chat_id
        ORDER BY last_date DESC
        LIMIT ?
    """, [limit])

    results = []
    for row in cursor.fetchall():
        results.append({
            "chat_id": row["chat_id"],
            "message_count": row["message_count"],
            "active_users": row["active_users"],
            "topic_count": row["topic_count"],
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        })
    conn.close()
    return results


@mcp.tool()
async def get_chat_topics(chat_id: int) -> list[dict]:
    """
    Список топиков (тредов) в конкретном чате.

    Args:
        chat_id: Числовой ID чата (например -1001234567890)

    Returns:
        По каждому топику: thread_id, message_count, active_users,
        first_date, last_date, sample_content (первое сообщение топика)
    """
    chat_id = normalize_chat_id(chat_id)
    conn = get_db()

    # Проверка, что чат существует
    chat = conn.execute("SELECT COUNT(*) as c FROM messages WHERE chat_id = ?", [chat_id]).fetchone()
    if not chat or chat["c"] == 0:
        conn.close()
        return []

    # Топики: группировка по thread_id. Для NULL (без топиков) — общий "General"
    cursor = conn.execute("""
        SELECT COALESCE(thread_id, 0) as tid,
               COUNT(*) as message_count,
               COUNT(DISTINCT user_id) as active_users,
               MIN(date) as first_date,
               MAX(date) as last_date
        FROM messages
        WHERE chat_id = ?
        GROUP BY tid
        ORDER BY last_date DESC
    """, [chat_id])

    topics = []
    for row in cursor.fetchall():
        tid = row["tid"]
        topic = {
            "chat_id": chat_id,
            "thread_id": None if tid == 0 else tid,
            "is_general": tid == 0,
            "message_count": row["message_count"],
            "active_users": row["active_users"],
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        }

        # Название топика: первое (по дате) содержательное сообщение
        sample = conn.execute("""
            SELECT content FROM messages
            WHERE chat_id = ? AND COALESCE(thread_id, 0) = ?
              AND content IS NOT NULL AND content != ''
            ORDER BY date ASC
            LIMIT 1
        """, [chat_id, tid]).fetchone()
        topic["sample_content"] = (sample["content"][:100] if sample else "")

        topics.append(topic)

    conn.close()
    return topics


@mcp.tool()
async def get_user_activity(
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    period: str = "month",
) -> dict:
    """
    Активность пользователя: где пишет, сколько, в каких чатах и топиках.

    Args:
        user_id: ID пользователя (или username)
        username: Username пользователя (или user_id)
        period: "day" (сегодня), "week" (7 дней), "month" (30 дней), "all"

    Returns:
        Профиль + статистика: total_messages, per_chat, last_activity, top_topics
    """
    if user_id is None and username is None:
        raise ToolError("Укажите user_id или username")

    conn = get_db()

    # Найти пользователя
    if user_id is not None:
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
    else:
        user_row = conn.execute("SELECT * FROM users WHERE username = ?", [username.lstrip("@")]).fetchone()

    if not user_row:
        conn.close()
        raise ToolError(f"Пользователь не найден (user_id={user_id}, username={username})")

    # Период
    now = datetime.utcnow()
    if period == "day":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    elif period == "month":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    elif period == "all":
        date_from = datetime(2000, 1, 1)
    else:
        raise ToolError(f"Неверный период: {period}. Ожидается: day, week, month, all")
    date_from_str = date_from.strftime("%Y-%m-%d %H:%M:%S")

    # Общее число сообщений за период
    total = conn.execute("""
        SELECT COUNT(*) as c FROM messages
        WHERE user_id = ? AND date >= ?
    """, [user_row["id"], date_from_str]).fetchone()["c"]

    # Последняя активность
    last = conn.execute("""
        SELECT date, chat_id, content FROM messages
        WHERE user_id = ? AND content IS NOT NULL AND content != ''
        ORDER BY date DESC LIMIT 1
    """, [user_row["id"]]).fetchone()

    # Разбивка по чатам
    per_chat = []
    for row in conn.execute("""
        SELECT chat_id, COUNT(*) as message_count,
               MAX(date) as last_date
        FROM messages
        WHERE user_id = ? AND date >= ?
        GROUP BY chat_id ORDER BY message_count DESC
    """, [user_row["id"], date_from_str]):
        per_chat.append({
            "chat_id": row["chat_id"],
            "message_count": row["message_count"],
            "last_date": row["last_date"],
        })

    # Топ топиков
    top_topics = []
    for row in conn.execute("""
        SELECT chat_id, thread_id, COUNT(*) as message_count
        FROM messages
        WHERE user_id = ? AND date >= ? AND thread_id IS NOT NULL
        GROUP BY chat_id, thread_id ORDER BY message_count DESC LIMIT 10
    """, [user_row["id"], date_from_str]):
        top_topics.append({
            "chat_id": row["chat_id"],
            "thread_id": row["thread_id"],
            "message_count": row["message_count"],
        })

    conn.close()

    return {
        "user": {
            "id": user_row["id"],
            "username": user_row["username"],
            "first_name": user_row["first_name"],
            "last_name": user_row["last_name"],
            "tags": user_row["tags"],
        },
        "period": period,
        "total_messages": total,
        "last_activity": {
            "date": last["date"] if last else None,
            "chat_id": last["chat_id"] if last else None,
            "content": last["content"][:100] if last and last["content"] else None,
        } if last else None,
        "per_chat": per_chat,
        "top_topics": top_topics,
    }


@mcp.tool()
async def get_chat_activity(
    chat_id: int,
    thread_id: Optional[int] = None,
    days: int = 30,
) -> list[dict]:
    """
    Активность чата (или топика) по дням — для трендов.

    Args:
        chat_id: Числовой ID чата
        thread_id: Фильтр по топику (None = весь чат)
        days: Сколько дней назад начать (по умолчанию 30)

    Returns:
        По дням: date, message_count, active_users
    """
    if days < 1 or days > 365:
        raise ToolError("days должен быть от 1 до 365")

    chat_id = normalize_chat_id(chat_id)
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from_str = date_from.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    sql = """
        SELECT substr(date, 1, 10) as day,
               COUNT(*) as message_count,
               COUNT(DISTINCT user_id) as active_users
        FROM messages
        WHERE chat_id = ? AND date >= ?
    """
    params = [chat_id, date_from_str]
    if thread_id is not None:
        sql += " AND thread_id = ?"
        params.append(thread_id)
    sql += " GROUP BY day ORDER BY day"

    results = []
    for row in conn.execute(sql, params):
        results.append({
            "date": row["day"],
            "message_count": row["message_count"],
            "active_users": row["active_users"],
        })
    conn.close()
    return results


@mcp.tool()
async def get_message_by_id(message_id: int, chat_id: int) -> dict:
    """
    Получить конкретное сообщение по id и chat_id.

    Args:
        message_id: ID сообщения
        chat_id: ID чата (составной ключ: (id, chat_id))

    Returns:
        Полные данные сообщения + автор + медиа
    """
    chat_id = normalize_chat_id(chat_id)
    conn = get_db()
    row = conn.execute("""
        SELECT m.*, u.username, u.first_name, u.last_name,
               md.type as media_type, md.url as media_url, md.title as media_title,
               md.description as media_description, md.thumb as media_thumb
        FROM messages m
        LEFT JOIN users u ON u.id = m.user_id
        LEFT JOIN media md ON md.id = m.media_id
        WHERE m.id = ? AND m.chat_id = ?
    """, [message_id, chat_id]).fetchone()
    conn.close()

    if not row:
        raise ToolError(f"Сообщение не найдено: id={message_id}, chat_id={chat_id}")

    result = {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "thread_id": row["thread_id"],
        "type": row["type"],
        "date": row["date"],
        "edit_date": row["edit_date"],
        "content": row["content"],
        "reply_to": row["reply_to"],
        "user_id": row["user_id"],
        "author": {
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
        },
    }
    if row["media_id"] is not None:
        result["media"] = {
            "id": row["media_id"],
            "type": row["media_type"],
            "url": row["media_url"],
            "title": row["media_title"],
            "description": row["media_description"],
            "thumb": row["media_thumb"],
        }
    return result


@mcp.tool()
async def get_recent_messages(
    chat_id: Optional[int] = None,
    thread_id: Optional[int] = None,
    limit: int = 20,
) -> list[dict]:
    """
    Последние сообщения без полнотекстового поиска.

    Args:
        chat_id: Фильтр по чату (None = все чаты)
        thread_id: Фильтр по топику (None = все)
        limit: Сколько сообщений (по умолчанию 20, максимум 100)

    Returns:
        Список сообщений: id, chat_id, thread_id, content, user_id, date, author
    """
    if limit < 1 or limit > 100:
        raise ToolError("limit должен быть от 1 до 100")

    if chat_id is not None:
        chat_id = normalize_chat_id(chat_id)
    conn = get_db()
    sql = """
        SELECT m.id, m.chat_id, m.thread_id, m.content, m.user_id, m.date, m.reply_to,
               u.username, u.first_name, u.last_name
        FROM messages m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE 1=1
    """
    params = []
    if chat_id is not None:
        sql += " AND m.chat_id = ?"
        params.append(chat_id)
    if thread_id is not None:
        sql += " AND m.thread_id = ?"
        params.append(thread_id)
    sql += " ORDER BY m.date DESC, m.id DESC LIMIT ?"
    params.append(limit)

    results = []
    for row in conn.execute(sql, params):
        results.append({
            "id": row["id"],
            "chat_id": row["chat_id"],
            "thread_id": row["thread_id"],
            "content": row["content"],
            "user_id": row["user_id"],
            "date": row["date"],
            "reply_to": row["reply_to"],
            "author": {
                "username": row["username"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
            },
        })
    conn.close()
    return results


@mcp.tool()
async def get_media(
    chat_id: Optional[int] = None,
    media_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Список медиа из сообщений (ссылки, фото, документы, опросы).

    Args:
        chat_id: Фильтр по чату
        media_type: Фильтр по типу: "photo", "webpage", "poll", "document"
        limit: Максимум записей (по умолчанию 50)

    Returns:
        По каждому медиа: id, type, url, title, description, thumb, chat_id, date
    """
    if limit < 1 or limit > 200:
        raise ToolError("limit должен быть от 1 до 200")

    if chat_id is not None:
        chat_id = normalize_chat_id(chat_id)
    conn = get_db()
    sql = """
        SELECT md.id, md.type, md.url, md.title, md.description, md.thumb,
               m.chat_id, m.date
        FROM media md
        JOIN messages m ON m.media_id = md.id
        WHERE 1=1
    """
    params = []
    if chat_id is not None:
        sql += " AND m.chat_id = ?"
        params.append(chat_id)
    if media_type is not None:
        sql += " AND md.type = ?"
        params.append(media_type)
    sql += " ORDER BY m.date DESC LIMIT ?"
    params.append(limit)

    results = []
    for row in conn.execute(sql, params):
        results.append({
            "id": row["id"],
            "type": row["type"],
            "url": row["url"],
            "title": row["title"],
            "description": row["description"],
            "thumb": row["thumb"],
            "chat_id": row["chat_id"],
            "date": row["date"],
        })
    conn.close()
    return results


@mcp.tool()
async def list_available_chats() -> list[dict]:
    """
    Список ВСЕХ чатов аккаунта через tg-archive --list-chats.
    Показывает все доступные диалоги (группы и каналы), даже не синхронизированные.

    Returns:
        По каждому чату: id, username, title
    """
    import asyncio
    import re

    def _list():
        log = run_tg_archive_sync("--list-chats")
        chats = []
        for m in re.finditer(r"id=(-?\d+)\s+username=(\S+)\s+title=(.*)", log):
            cid, uname, title = m.groups()
            chats.append({
                "id": int(cid),
                "username": None if uname in ("-", "None") else uname,
                "title": title.strip(),
            })
        if not chats:
            raise ToolError(
                "Не удалось получить список чатов (нужна авторизация?). Лог:\n" + log[-1000:]
            )
        return chats

    return await asyncio.to_thread(_list)


@mcp.tool()
async def add_chat(chat_ref: str) -> dict:
    """
    Добавить чат в список синхронизации (config.yaml).

    Args:
        chat_ref: ID чата (-100123), t.me URL (https://t.me/group),
                  @username или display name

    Returns:
        Текущий список chats + подтверждение
    """
    ref = normalize_chat_ref(chat_ref)
    # Числовые ссылки храним как int (формат config.yaml tg-archive)
    ref_value = int(ref) if ref.lstrip("-").isdigit() else ref

    chats = get_chats_from_config()
    if any(str(c) == str(ref) for c in chats):
        return {"added": False, "message": f"Чат уже в списке: {ref}", "chats": chats}

    chats.append(ref_value)
    set_chats_in_config(chats)

    return {
        "added": True,
        "chat_ref": ref,
        "message": f"Чат {ref} добавлен. Запустите run_sync для синхронизации.",
        "chats": get_chats_from_config(),
    }


@mcp.tool()
async def remove_chat(chat_ref: str) -> dict:
    """
    Убрать чат из списка синхронизации (config.yaml).

    Args:
        chat_ref: ID чата, @username или display name (как указан в config)

    Returns:
        Обновлённый список chats
    """
    ref = normalize_chat_ref(chat_ref)

    chats = get_chats_from_config()
    if not any(str(c) == str(ref) for c in chats):
        return {"removed": False, "message": f"Чат не в списке: {ref}", "chats": chats}

    chats = [c for c in chats if str(c) != str(ref)]
    set_chats_in_config(chats)

    return {
        "removed": True,
        "chat_ref": ref,
        "chats": get_chats_from_config(),
    }


@mcp.tool()
async def run_sync() -> dict:
    """
    Запустить синхронизацию tg-archive (--sync) через Docker.

    Скачивает новые сообщения из всех чатов, указанных в config.yaml.

    Returns:
        Статус + последние строки лога синхронизации
    """
    chats = get_chats_from_config()
    if not chats:
        raise ToolError("Список chats пуст. Сначала добавьте чат: add_chat")

    log = run_tg_archive_sync("--sync")

    return {
        "status": "completed",
        "chats": chats,
        "log": log,
    }


# --- Авторизация через MCP: прокси интерактивного входа tg-archive ---
# Контейнер watchdog-tg-archive запускается с открытым stdin (--sync).
# MCP пишет в stdin телефон/код/пароль и читает промпты Telethon из stdout.
# Сессию создаёт сам tg-archive — та же версия Telethon, что и при sync,
# поэтому проблемы несовместимости сессий исключены.

_auth_container = None
_auth_sock = None
_auth_buf = ""
_auth_scan_pos = 0


def _stop_auth_container():
    """Остановить и убрать контейнер авторизации, если запущен."""
    global _auth_container, _auth_sock, _auth_scan_pos
    if _auth_sock is not None:
        try:
            _auth_sock.close()
        except Exception:
            pass
        _auth_sock = None
    if _auth_container is not None:
        try:
            _auth_container.remove(force=True)
        except Exception:
            pass
        _auth_container = None
    _auth_scan_pos = 0


def _auth_write(text: str):
    """Записать строку в stdin контейнера авторизации."""
    _auth_sock._sock.sendall((text + "\n").encode())


def _auth_read_until(patterns, timeout=45):
    """
    Читать stdout/stderr контейнера до СВЕЖЕГО совпадения с одним из паттернов.
    Совпадения ищутся только после позиции, найденной в прошлый вызов
    (_auth_scan_pos), чтобы старые промпты не детектились повторно.
    """
    global _auth_buf, _auth_scan_pos
    import socket as socket_mod
    import time as time_mod

    _auth_sock._sock.settimeout(3)
    start = time_mod.time()
    while time_mod.time() - start < timeout:
        try:
            chunk = _auth_sock._sock.recv(4096)
            if chunk:
                _auth_buf += chunk.decode("utf-8", errors="replace")
        except socket_mod.timeout:
            pass
        except OSError:
            break
        for p in patterns:
            idx = _auth_buf.find(p, _auth_scan_pos)
            if idx != -1:
                _auth_scan_pos = idx + len(p)
                return p
    return None


def _authorize_sync(phone: str, code, password) -> dict:
    """Синхронная реализация авторизации через контейнер tg-archive."""
    global _auth_container, _auth_sock, _auth_buf, _auth_scan_pos
    import shutil
    import time as time_mod

    import docker

    phone = phone.strip().lstrip("+")
    if not phone or not phone.isdigit():
        raise ToolError(f"Неверный номер телефона: {phone}. Формат: +79261234567")

    # --- Шаг 1: старт новой авторизации (без code) ---
    if not code:
        _stop_auth_container()

        # Новая авторизация = новая сессия. Бэкапим и удаляем старую
        # (иначе битая/чужая сессия уронит tg-archive до промпта).
        if Path(SESSION_PATH).exists():
            shutil.copy(SESSION_PATH, SESSION_PATH + ".bak")
            Path(SESSION_PATH).unlink()
        for suffix in ("-journal", "-wal", "-shm"):
            extra = Path(SESSION_PATH + suffix)
            if extra.exists():
                extra.unlink()

        try:
            client = docker.from_env()
        except Exception as e:
            raise ToolError(f"Docker недоступен: {e}")

        try:
            _auth_container = client.containers.create(
                image="watchdog-tg-archive:latest",
                command=["--sync"],
                volumes={"telegram_data": {"bind": "/data", "mode": "rw"}},
                stdin_open=True,
                detach=True,
            )
            _auth_container.start()
            _auth_sock = _auth_container.attach_socket(
                params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1, "logs": 1}
            )
            _auth_buf = ""
            _auth_scan_pos = 0
        except docker.errors.ImageNotFound:
            raise ToolError(
                "Образ watchdog-tg-archive не найден. Соберите: docker compose build tg-archive"
            )

        # Telethon спросит телефон — вводим
        time_mod.sleep(2)
        _auth_write(phone)

        p = _auth_read_until(
            ["Please enter the code you received", "Please enter your password"],
            timeout=60,
        )
        if p == "Please enter the code you received":
            return {
                "status": "code_sent",
                "phone": phone,
                "message": "Код отправлен в Telegram. Вызовите authorize с code.",
            }
        if p == "Please enter your password":
            return {
                "status": "password_needed",
                "message": "Сессия восстановлена, включён 2FA. Вызовите authorize с code и password.",
            }

        # Промпт не получен — контейнер мог упасть, достаём лог
        try:
            _auth_container.reload()
            if _auth_container.status in ("exited", "dead"):
                logs = _auth_container.logs().decode("utf-8", errors="replace")
                _stop_auth_container()
                raise ToolError(f"tg-archive завершился до запроса кода. Лог:\n{logs[-1500:]}")
        except docker.errors.NotFound:
            _stop_auth_container()
            raise ToolError(f"Контейнер авторизации умер. Вывод:\n{_auth_buf[-800:]}")

        return {
            "status": "unknown",
            "message": "Промпт кода не получен за 60с. Повторите authorize(phone).",
            "output": _auth_buf[-800:],
        }

    # --- Шаг 2+: ввод кода (и пароля, если передан) ---
    if _auth_container is None or _auth_sock is None:
        raise ToolError("Авторизация не начата. Сначала вызовите authorize(phone) без code.")

    _auth_write(code.strip())

    # Что дальше: пароль, успешный вход (sync пошёл), или повторный запрос кода
    p = _auth_read_until(
        [
            "Please enter your password",
            "fetching from",
            "finished. fetched",
            "Please enter the code you received",
        ],
        timeout=60,
    )

    if p == "Please enter the code you received":
        return {"status": "code_invalid", "message": "Неверный код. Введите код ещё раз (authorize с code)."}

    if p == "Please enter your password":
        if not password:
            return {"status": "password_needed", "message": "Включён 2FA. Вызовите authorize с code и password."}
        _auth_write(password)
        p2 = _auth_read_until(
            ["fetching from", "finished. fetched", "Please enter your password"],
            timeout=60,
        )
        if p2 == "Please enter your password":
            return {"status": "password_invalid", "message": "Неверный пароль 2FA. Введите пароль ещё раз."}
        if p2 in ("fetching from", "finished. fetched"):
            return _auth_success()
        return {
            "status": "unknown",
            "message": "Не удалось подтвердить вход после пароля.",
            "output": _auth_buf[-800:],
        }

    if p in ("fetching from", "finished. fetched"):
        return _auth_success()

    return {
        "status": "unknown",
        "message": "Не удалось подтвердить вход после кода.",
        "output": _auth_buf[-800:],
    }


def _auth_success() -> dict:
    """Успешный вход: закрыть сокет, контейнер дорабатывает sync сам."""
    global _auth_sock
    if _auth_sock is not None:
        try:
            _auth_sock.close()
        except Exception:
            pass
        _auth_sock = None
    return {
        "status": "authorized",
        "message": "Авторизация успешна. Сессия сохранена tg-archive; sync продолжается в фоне.",
    }


@mcp.tool()
async def authorize(
    phone: str,
    code: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """
    Авторизация Telegram через tg-archive (интерактивный вход в контейнере).

    Flow (state хранится между вызовами в одном процессе):

    1. authorize(phone="+79261234567") — запускает контейнер tg-archive,
       вводит телефон, Telegram отправляет SMS-код.
    2. authorize(phone, code="12345") — вводит код.
       При включённом 2FA вернёт password_needed.
    3. authorize(phone, code="12345", password="secret") — завершает 2FA.

    Args:
        phone: Номер телефона в международном формате, например +79261234567
        code: SMS-код из Telegram (при первом вызове не нужен)
        password: Пароль 2FA, если включён

    Returns:
        status: "code_sent" (нужен code), "password_needed" (нужен password),
                "authorized" (сессия сохранена, sync идёт в фоне),
                "code_invalid" / "password_invalid" (повторите ввод)
    """
    import asyncio

    return await asyncio.to_thread(_authorize_sync, phone, code, password)


def main():
    """Точка входа."""
    transport = os.environ.get("WATCHDOG_TRANSPORT", "streamable-http")
    host = os.environ.get("WATCHDOG_HOST", "0.0.0.0")
    port = int(os.environ.get("WATCHDOG_PORT", "8040"))

    logger.info("Запуск Watchdog MCP сервера...")
    logger.info(f"Путь к БД: {DB_PATH}")
    logger.info(f"Транспорт: {transport}, host: {host}, port: {port}")

    init_db()

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
