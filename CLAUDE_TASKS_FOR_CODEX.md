# CLAUDE_TASKS_FOR_CODEX

> Живая очередь заданий для Codex от Claude. Обновляется после каждого
> выполненного фронт-пункта. Дата обновления: 2026-07-05 (пункты 1-2 закрыты).

## Очередь (по приоритету)

### ~~1. Механизм включения live-букинга per-salon~~ · ✅ ЗАКРЫТО 2026-07-05
(бэк Codex + панель в админке Claude: requested/effective/blockers + тумблер)
- Endpoint: нет (нужен способ перевести салон в `booking_mode='live'`)
- Current: `booking_mode` всегда `preview`; фронт-переключатель уже готов
  и ждёт только флага из конфига
- Desired: правило/эндпоинт (например, `active`-салон с подключённой real-CRM
  → live; или тумблер в `PATCH /admin/tenants/:id`) + отражение в
  `GET /mobile/config/:slug`
- Фронт после этого: ничего менять не нужно — подхватит сам

### ~~2. SMS-транспорт для phone-auth~~ · ✅ ЗАКРЫТО 2026-07-05
- Endpoint: `POST /auth/phone/start`
- Status: добавлен transport-layer `debug | smsru`, `delivery: 'sms'` в проде
  при валидном `SMSRU_API_ID`, `debug_code` скрывается вне debug-режима
- Safe mode: локально и в test при `PHONE_AUTH_PROVIDER=auto` всё остаётся
  в `delivery: 'debug'`
- Фронт: ничего менять не нужно, текущая логика Claude уже совместима

### ~~3. Биллинг подписок (ручной минимум)~~ · ✅ ЗАКРЫТО 2026-07-05
(бэк Codex: поля billing + пересчёт grace; панель «Подписка» в админке Claude)

### ~~3b. Биллинг: реальная оплата~~ · ✅ BACKEND ЗАКРЫТ 2026-07-05
(бэк Codex: YooKassa checkout, webhook, сохранение payment_method, recurring-charge endpoint, run-due/past_due; UI-пакет для Claude: `CLAUDE_YOOKASSA_BILLING_PACKET.md`)

### 4. Upload логотипа файлом · non-blocking
- Endpoint: нет (`POST /admin/tenants/:id/logo`?)
- Current: в админке поле URL
- Desired: multipart-загрузка + отдача статики; фронт добавит file-input

### 5. Деплой NestJS-бэка + поддомены салонов · blocking для реальных клиентов
- Current: всё на localhost:3000
- Desired: план деплоя (сервер/домен/SSL) — согласуем со Стасом

### 6. OAuth-ключи провайдеров (Яндекс/Telegram) + redirect whitelist · blocking для соц-входа
- Current: локальный env без ключей → `/auth/oauth/*/start` = `social_provider_unavailable`
- Desired: клиент-id/секреты Яндекс+Telegram в env; в whitelist redirect_uri
  добавить `http://127.0.0.1:8787/oauth-callback.html` (локально) и
  `https://malesthetic.pro/app/oauth-callback.html` (прод)
- Фронт соц-входа готов (кнопки + callback-страница), ждёт только ключи
