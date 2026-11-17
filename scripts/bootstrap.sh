#!/usr/bin/env bash
#
# Watchdog bootstrap — настройка проекта с нуля.
#
# Использование:
#   ./scripts/bootstrap.sh            полный сетап: проверки → env → build → auth → sync → verify
#   ./scripts/bootstrap.sh --check    только проверки окружения
#   ./scripts/bootstrap.sh --build    пересобрать образы
#   ./scripts/bootstrap.sh --sync     инкрементальная синхронизация
#   ./scripts/bootstrap.sh --chats    список доступных чатов авторизованного аккаунта
#   ./scripts/bootstrap.sh --verify   проверка MCP-сервера (HTTP handshake)
#
# Скрипт идемпотентен: повторный запуск делает только недостающие шаги.
# Первый --sync интерактивен: Telethon запрашивает телефон, SMS-код и (при 2FA) пароль.
# Запускать в PTY-терминале.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TG="watchdog-tg-archive:latest"
IMAGE_MCP="watchdog-mcp:latest"

say()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- проверки окружения ----------

check_docker() {
    command -v docker >/dev/null || fail "docker не установлен"
    docker info >/dev/null 2>&1 || fail "docker-демон не отвечает (запущен ли Docker?)"
    docker compose version >/dev/null 2>&1 || fail "docker compose v2 не найден"
    ok "docker и docker compose доступны"
}

check_env() {
    [ -f .env ] || { cp .env.example .env; say "создан .env из .env.example — заполни TG_API_ID, TG_API_HASH, TG_PHONE, TG_CHAT_IDS"; }
    local missing=0
    for var in TG_API_ID TG_API_HASH TG_PHONE; do
        if [ -z "$(grep -E "^${var}=." .env || true)" ]; then
            say "переменная ${var} не заполнена в .env"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] || fail "заполни .env (см. .env.example) и запусти снова"
    ok ".env заполнен"
}

check_session() {
    docker run --rm -v telegram_data:/data alpine test -f /data/session.session >/dev/null 2>&1
}

check_db() {
    docker run --rm -v telegram_data:/data alpine test -f /data/data.sqlite >/dev/null 2>&1
}

# ---------- шаги ----------

build_images() {
    if [ "${1:-}" = "force" ] || ! docker image inspect "$IMAGE_TG" >/dev/null 2>&1 \
       || ! docker image inspect "$IMAGE_MCP" >/dev/null 2>&1; then
        say "сборка образов..."
        docker compose build tg-archive watchdog-mcp
        ok "образы собраны: ${IMAGE_TG}, ${IMAGE_MCP}"
    else
        ok "образы уже собраны (пересборка: ./scripts/bootstrap.sh --build)"
    fi
}

first_sync() {
    if check_session; then
        ok "сессия Telegram уже существует (auth пропущен)"
        return
    fi
    say "первый запуск — интерактивная авторизация Telethon"
    say "понадобятся: телефон (возьмётся из .env), SMS-код, при 2FA — пароль"
    docker compose run --rm tg-archive --sync
    check_session && ok "авторизация прошла, session.session сохранён в volume" \
        || fail "авторизация не завершена: session.session не найден"
}

incremental_sync() {
    check_session || fail "нет сессии — сначала полный сетап (./scripts/bootstrap.sh)"
    docker compose run --rm tg-archive --sync
    ok "синхронизация завершена"
}

list_chats() {
    check_session || fail "нет сессии — сначала полный сетап"
    docker compose run --rm tg-archive --sync --list-chats
}

verify_mcp() {
    docker image inspect "$IMAGE_MCP" >/dev/null 2>&1 || fail "образ ${IMAGE_MCP} не собран"
    command -v curl >/dev/null || fail "curl не установлен"
    say "запускаю MCP-сервис и проверяю HTTP handshake..."
    docker compose up -d watchdog-mcp

    local resp=""
    for _ in {1..30}; do
        resp=$(curl -sS --max-time 5 -X POST http://127.0.0.1:8040/mcp \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"bootstrap","version":"1.0"}}}' \
            2>/dev/null || true)
        if grep -q '"serverInfo"' <<<"$resp"; then
            ok "MCP-сервер отвечает по HTTP: http://127.0.0.1:8040/mcp"
            check_db && ok "база данных на месте (data.sqlite в volume)" || say "база пуста — чаты ещё не синхронизировались"
            return
        fi
        sleep 1
    done

    docker compose logs --tail=20 watchdog-mcp >&2
    fail "MCP-сервер не ответил на initialize"
}

# ---------- маршрутизация ----------

case "${1:-}" in
    --check)
        check_docker; check_env
        check_session && ok "сессия есть" || say "сессии нет — нужен первый --sync"
        check_db && ok "база есть" || say "базы нет — нужен --sync"
        ;;
    --build)
        check_docker; check_env; build_images force
        ;;
    --sync)
        check_docker; check_env; incremental_sync
        ;;
    --chats)
        check_docker; check_env; list_chats
        ;;
    --verify)
        check_docker; verify_mcp
        ;;
    "")
        check_docker; check_env; build_images; first_sync; verify_mcp
        say "готово. Подключение MCP-клиента — см. README.md, раздел «Подключение MCP-клиента»"
        ;;
    *)
        fail "неизвестный аргумент: $1 (см. шапку скрипта)"
        ;;
esac
