# CLAUDE_TASKS_FOR_CODEX

> Живая очередь заданий для Codex от Claude. Обновляется после каждого
> выполненного фронт-пункта. Дата: 2026-07-05.

## Очередь (по приоритету)

### 1. Механизм включения live-букинга per-salon · blocking для запуска
- Endpoint: нет (нужен способ перевести салон в `booking_mode='live'`)
- Current: `booking_mode` всегда `preview`; фронт-переключатель уже готов
  и ждёт только флага из конфига
- Desired: правило/эндпоинт (например, `active`-салон с подключённой real-CRM
  → live; или тумблер в `PATCH /admin/tenants/:id`) + отражение в
  `GET /mobile/config/:slug`
- Фронт после этого: ничего менять не нужно — подхватит сам

### 2. SMS-транспорт для phone-auth · blocking для прода
- Endpoint: `POST /auth/phone/start`
- Current: `delivery: debug`, код на экране
- Desired: реальная отправка (провайдер за Стасом), `delivery: 'sms'`,
  `debug_code` отсутствует в проде
- Фронт готов: debug-чип показывается только при `delivery === 'debug'`

### 3. Биллинг подписок · blocking для денег
- Current: тарифы есть, списаний/продления/`past_due`-переходов нет
- Desired: минимум — ручной перевод тарифа/статуса + даты периода;
  максимум — ЮKassa-автосписание (см. черновик
  `maya-saas-blueprint/pg/tenant_billing.py`)

### 4. Upload логотипа файлом · non-blocking
- Endpoint: нет (`POST /admin/tenants/:id/logo`?)
- Current: в админке поле URL
- Desired: multipart-загрузка + отдача статики; фронт добавит file-input

### 5. Деплой NestJS-бэка + поддомены салонов · blocking для реальных клиентов
- Current: всё на localhost:3000
- Desired: план деплоя (сервер/домен/SSL) — согласуем со Стасом
