FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код сервера
COPY server.py .

# Данные: SQLite от tg-archive
VOLUME ["/data"]

# MCP работает через stdio
CMD ["python", "server.py"]
