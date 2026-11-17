FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код сервера
COPY server.py .

# Данные: SQLite от tg-archive
VOLUME ["/data"]

# По умолчанию MCP слушает Streamable HTTP на порту 8040
EXPOSE 8040
CMD ["python", "server.py"]
