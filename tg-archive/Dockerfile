FROM python:3.11-slim

WORKDIR /app

# Установка tg-archive из форка с поддержкой нескольких чатов и топиков.
# Форк приватный, поэтому исходники вендорятся в vendor/tg-archive-fork
# (gitignored; обновление: git clone https://github.com/9866494/tg-archive-fork.git
# в tg-archive/vendor/tg-archive-fork).
COPY vendor/tg-archive-fork /tmp/tg-archive-fork
RUN pip install --no-cache-dir /tmp/tg-archive-fork \
    && rm -rf /tmp/tg-archive-fork

# Entrypoint для генерации config.yaml из env
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Данные: SQLite, session, media
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--sync"]
