FROM python:3.11-slim

# Пин на конкретный коммит (нет релизов у tg-archive)
# https://github.com/knadh/tg-archive/commit/master
ARG TG_ARCHIVE_COMMIT=master

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Установка tg-archive из git с пином на коммит
RUN pip install --no-cache-dir git+https://github.com/knadh/tg-archive.git@${TG_ARCHIVE_COMMIT}

# Entrypoint для генерации config.yaml из env
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Данные: SQLite, session, media
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--sync"]
