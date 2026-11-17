# Задача: watchdog-mcp — постоянный HTTP-сервис вместо stdio-контейнеров

Дата: 2026-11-17. Источник задачи: разбор утечки watchdog-контейнеров на e5-2650.

## Диагноз (проверено фактами)

- watchdog-mcp (server.py, Python SDK `mcp` 2.3.0, класс `MCPServer`) работает как stdio-сервер.
- Клиенты спавнят его по контейнеру на сессию: `docker run -i --rm` — в трёх местах:
  `~/.kimi-code/mcp.json`, `~/.config/opencode/opencode.json`, `./opencode.json` этого репо.
- Наблюдаемая утечка: 27 контейнеров, возраст до 4 дней. Причины:
  1. kimi-code выживает при обрыве SSH (осиротевшие процессы на мёртвых TTY), его
     docker run клиенты живут, контейнеры копятся. `--rm` срабатывает только при
     завершении контейнера, а контейнеры не завершаются.
  2. Kimi Code автоматически переподключает упавший MCP с exponential backoff
     (документировано: 5s → 300s). Остановка контейнера приводит к его пересозданию,
     поэтому чистка бессмысленна, пока жива сессия клиента.
- Проверено в образе: SDK 2.3.0 умеет `MCPServer.run(transport="stdio"|"sse"|"streamable-http")`.
- Документация Kimi Code рекомендует HTTP для сервисов, живущих постоянно.
  В `~/.kimi-code/mcp.json` уже есть HTTP-серверы по этому паттерну (graphiti, workspace-mcp).

## Цель

watchdog-mcp работает как один постоянный HTTP-сервис (streamable-http) на
`127.0.0.1:8040/mcp`. Клиенты подключаются по URL. Per-session контейнеры исчезают,
утечка становится невозможной конструктивно.

## Изменения

1. `watchdog-mcp/server.py`:
   - transport из env `WATCHDOG_TRANSPORT` (default `streamable-http`), stdio остаётся
     доступным через `WATCHDOG_TRANSPORT=stdio` (обратная совместимость);
   - host из env `WATCHDOG_HOST` (default `0.0.0.0`), port из env `WATCHDOG_PORT` (default `8040`);
   - логировать выбранный transport/host/port при старте.
2. `docker-compose.yml`:
   - сервис `watchdog-mcp` — постоянный: убрать `profiles`, добавить `restart: unless-stopped`,
     `ports: ["127.0.0.1:8040:8040"]`, env `WATCHDOG_DB_PATH`, `WATCHDOG_HOST=0.0.0.0`,
     `WATCHDOG_PORT=8040`;
   - volumes как сейчас: `telegram_data:/data` и docker.sock (нужен tools `authorize`/`run_sync`);
   - обновить комментарий в шапке сервиса.
3. Клиентские конфиги (вне репо; перед каждой правкой бэкап `<файл>.bak-http-20261117`):
   - `~/.kimi-code/mcp.json`: запись watchdog → `{"enabled": true, "url": "http://127.0.0.1:8040/mcp"}`
     (command/args удалить, timeout-поля можно сохранить);
   - `~/.config/opencode/opencode.json`: watchdog → `{"type": "remote", "url": "http://127.0.0.1:8040/mcp", "enabled": true}`;
   - `./opencode.json` этого репо: то же самое.
4. Документация: главный `README.md` и `AGENTS.md` — обновить схему подключения
   (endpoint `http://127.0.0.1:8040/mcp`, как поднять, как переключить клиентов).
5. Коммит(ы) в этом репо, сообщения на русском.

## Ограничения

- `tg-archive` не трогать.
- docker.sock оставить маунтом сервису.
- Остальные MCP-серверы в клиентских конфигах не менять.
- Тексты — по правилам AGENTS.md (русский, stop-slop).

## Критерии приёмки

1. Образ пересобран, сервис поднят и переживает `docker compose restart`: статус Up.
2. MCP жив по HTTP. Проверка хендшейка:
   `curl -sS -X POST http://127.0.0.1:8040/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"probe\",\"version\":\"0\"}}}"` —
   в ответе JSON-RPC result с serverInfo (формат ответа streamable-http может быть SSE-фреймом).
3. Порт слушается только на loopback: `ss -tlnp | grep 8040` показывает `127.0.0.1:8040`.
4. Конфиги клиентов переключены на URL, синтаксис JSON валиден (`python3 -m json.tool`).
5. Инструмент читает БД через HTTP: минимальный тест tools/list или вызов read-инструмента
   после initialize (можно скриптом на SDK-клиенте).
6. README и AGENTS.md обновлены, коммиты сделаны.

## Откат

`git revert` в этом репо + вернуть клиентские конфиги из `.bak-http-20261117` +
`docker compose up -d` со старой конфигурацией не требуется: stdio-путь остался в коде.
