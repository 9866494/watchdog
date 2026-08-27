"""
Watchdog MCP Server

MCP-сервер для мониторинга Telegram-чатов через данные tg-archive.
Читает SQLite БД и отдаёт MCP tools для поиска и анализа.

Схема БД (из tg-archive):
- messages: id, type, date, edit_date, content, reply_to, user_id, media_id
- users: id, username, first_name, last_name, tags, avatar
- media: id, type, url, title, description, thumb
"""

import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server import MCPServer

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


def get_db() -> sqlite3.Connection:
    """Получить соединение с БД."""
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(f"База данных не найдена: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

    # Проверка существования FTS5 индекса
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    )
    fts_exists = cursor.fetchone() is not None

    if not fts_exists:
        logger.info("Создаю FTS5 индекс...")

        # FTS5 индекс по content, user_id, date
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, user_id, date,
                content='messages', content_rowid='id'
            )
        """)

        # Rebuild индекса из существующих данных
        logger.info("Rebuild FTS5 индекса из существующих сообщений...")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

        # Триггер для автообновления FTS5 при новых сообщениях
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, user_id, date)
                VALUES (new.id, new.content, new.user_id, new.date);
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
    limit: int = 50,
) -> list[dict]:
    """
    Поиск сообщений по полнотекстовому запросу (FTS5).

    Args:
        query: Поисковый запрос (поддерживает FTS5 синтаксис: "точное совпадение", OR, NOT)
        date_from: Начальная дата (ISO 8601: "2024-01-01" или "2024-01-01T10:00:00")
        date_to: Конечная дата (ISO 8601)
        limit: Максимальное количество результатов (по умолчанию 50)

    Returns:
        Список сообщений: id, content, user_id, date, reply_to
    """
    conn = get_db()

    # Парсинг дат
    date_from_parsed = parse_date(date_from)
    date_to_parsed = parse_date(date_to)

    # Построение запроса
    sql = """
        SELECT m.id, m.content, m.user_id, m.date, m.reply_to,
               u.username, u.first_name, u.last_name
        FROM messages_fts fts
        JOIN messages m ON m.id = fts.rowid
        LEFT JOIN users u ON u.id = m.user_id
        WHERE messages_fts MATCH ?
    """
    params = [query]

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
        Статистика: message_count, active_users, top_authors (топ-10)
    """
    conn = get_db()

    # Определение диапазона дат
    now = datetime.utcnow()
    if period == "day":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from = date_from.replace(day=date_from.day - 6)
    elif period == "month":
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from = date_from.replace(day=date_from.day - 29)
    else:
        raise ValueError(f"Неверный период: {period}. Ожидается: day, week, month")

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

    conn.close()

    return {
        "period": period,
        "date_from": date_from_str,
        "message_count": message_count,
        "active_users": active_users,
        "top_authors": top_authors,
    }


def main():
    """Точка входа."""
    logger.info("Запуск Watchdog MCP сервера...")
    logger.info(f"Путь к БД: {DB_PATH}")

    # Инициализация БД
    init_db()

    # Запуск MCP-сервера через stdio
    logger.info("MCP-сервер запущен (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
