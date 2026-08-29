#!/bin/bash
set -e

# Рабочая директория — /data (volume)
cd /data

# Генерация config.yaml из env-переменных если его нет
if [ ! -f config.yaml ]; then
    echo "Создаю config.yaml из переменных окружения..."
    
    # Парсинг TG_CHAT_IDS (список через запятую) в YAML формат
    if [ -n "$TG_CHAT_IDS" ]; then
        # Преобразуем "id1,id2,id3" в YAML список
        CHATS_YAML=$(echo "$TG_CHAT_IDS" | tr ',' '\n' | sed 's/^/  - /')
        cat > config.yaml <<EOF
api_id: "${TG_API_ID}"
api_hash: "${TG_API_HASH}"
chats:
${CHATS_YAML}
site_name: "Watchdog"
site_url: ""
site_description: "Telegram chat monitoring"
site_tagline: ""
page_size: 100
fetch_batch_size: 5000
fetch_wait: 5
fetch_limit: 0
download_media: false
download_avatars: false
avatar_size: [64, 64]
media_dir: "media"
tz: "UTC"
use_takeout: false
EOF
    else
        # Fallback на одиночный group если TG_CHAT_IDS не задан
        cat > config.yaml <<EOF
api_id: "${TG_API_ID}"
api_hash: "${TG_API_HASH}"
group: ""
site_name: "Watchdog"
site_url: ""
site_description: "Telegram chat monitoring"
site_tagline: ""
page_size: 100
fetch_batch_size: 5000
fetch_wait: 5
fetch_limit: 0
download_media: false
download_avatars: false
avatar_size: [64, 64]
media_dir: "media"
tz: "UTC"
use_takeout: false
EOF
    fi
    echo "config.yaml создан"
fi

# Создаю директорию media если нужно
mkdir -p media

# Запуск tg-archive с переданными аргументами
exec tg-archive "$@"
