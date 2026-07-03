# SaaS Этап 0 — фундамент мультитенантности

Цель этапа: один код обслуживает много салонов. Текущий салон «Мужская
Эстетика» становится `tenant_id = 1` и продолжает работать без простоя.
Этот этап ничего не выкатывает в прод — это каркас и план. Файлы рядом:

- `tenants_schema.sql` — таблицы `tenants` / `tenant_invoices`, добавление
  `tenant_id` во все 31 доменную таблицу, Row-Level Security.
- `booking_provider.py` — интерфейс `BookingProvider` + `YClientsProvider`
  (обёртка вокруг текущего `yclients.py`) + фабрика `get_provider(tenant)`.

---

## 1. Что переезжает из `config.py` в строку тенанта

| Сейчас глобально в config.py | Куда | Поле в `tenants` |
|---|---|---|
| `BARBERSHOP_NAME/ADDRESS/CITY/PHONE/HOURS/2GIS/YANDEX`, `SITE_URL`, `APP_URL` | пер-тенант | `brand` (JSONB) |
| `YCLIENTS_COMPANY_ID`, `YCLIENTS_USER_TOKEN`, `YCLIENTS_CASH_ACCOUNT_ID`, `YCLIENTS_CASHLESS_ACCOUNT_ID` | пер-тенант, **шифр** | `provider_config` (BYTEA) |
| `ACTIVE_MASTER_IDS` | пер-тенант | `active_master_ids` |
| `TELEGRAM_TOKEN`, `BOT_USERNAME` | пер-тенант (для dedicated-бота), **шифр** | `bot_config` (BYTEA) |
| `INITIAL_ADMIN_IDS` | пер-тенант | `admin_tg_ids` |
| `YUKASSA_SHOP_ID/SECRET_KEY/PROVIDER_TOKEN` (ЮKassa салона для его сертов/абонементов) | пер-тенант, **шифр** | `payments_config` (BYTEA) |
| `VK_*`, `SMSRU_API_ID`, `REMINDER_MINUTES_BEFORE`, `PII_RETENTION_MONTHS` | пер-тенант | `settings` (JSONB) |

**Остаётся платформенным (в окружении сервиса, НЕ в тенанте):**
`YCLIENTS_PARTNER_TOKEN` (наш партнёрский — один на весь сервис),
`YCLIENTS_BASE_URL`, `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_MODEL`,
`PROXY_URL`, `PII_ENCRYPTION_KEY` (ключ шифрования — наш), `FAL_*`, `SERVER_COSTS_RUB`.

> Линия раздела по YClients: **partner-токен — наш**, `company_id` + `user_token`
> — салона. Именно поэтому подключение нового салона = он отдаёт нам доступ к
> своей компании (логином или 1-кликом в Маркетплейсе), а не мы заводим токены.

---

## 2. Как `tenant_id` течёт через работающий код

Не тащим `tenant_id` параметром в каждую функцию. Вводим **контекст тенанта**
через `contextvars` + тонкий слой доступа к данным:

- **Telegram (бот):** в начале обработки апдейта определяем тенанта
  (по `bot_config.token` для dedicated-ботов, либо по deep-link `?start=salon_<id>`
  / сохранённой привязке chat→tenant для общего бота) → `tenant_ctx.set(tid)`.
- **HTTP (webhook_server / PWA):** тенант по `Host` (поддомен `slug.app.ru`)
  или по сессии → `tenant_ctx.set(tid)` на время запроса.
- **Фоновые джобы:** сейчас глобальные (напоминания, дневной отчёт, лояльность,
  реактивация). Переписать на `for tenant in active_tenants(): tenant_ctx.set(...)`.
- **Слой данных `database.py`:** каждая функция читает `tenant_ctx.get()` и
  добавляет `tenant_id` в `WHERE`/`INSERT`. В Postgres дополнительно
  `SET app.tenant_id = <id>` на соединение → **RLS** не даст отдать чужое,
  даже если в коде забыли фильтр (страховка от утечки ПД между салонами).
- **Платформа записи:** `provider = get_provider(tenant)` вместо глобального
  `yclients`. Дальше код зовёт `provider.get_services(...)` и т.д.

---

## 3. Миграция SQLite → PostgreSQL (по шагам)

SQLite не тянет конкурентную запись из нескольких процессов (бот +
webhook_server + джобы) на много тенантов — Postgres обязателен.

1. **Поднять** Managed PostgreSQL (Yandex Cloud, РФ — 152-ФЗ ок).
2. **Схема:** перенести 31 `CREATE TABLE` (типы: `INTEGER PRIMARY KEY
   AUTOINCREMENT` → `BIGINT GENERATED ... IDENTITY`; даты → `TIMESTAMPTZ`;
   `BYTEA` для шифрованных ПД). Прогнать `tenants_schema.sql`
   (создаёт `tenants`/`tenant_invoices`, добавляет `tenant_id`, включает RLS).
3. **Создать tenant 1** = «Мужская Эстетика», перенести текущий `config.py` в
   его строку (`brand`/`provider_config`/`bot_config`/... зашифровать секреты).
4. **Бэкфилл данных:** выгрузить `barbershop.db` → залить в Postgres со
   `tenant_id = 1` (скрипт построчно; ПД уже зашифрованы тем же Fernet-ключом).
5. **Код:** заменить слой соединения в `database.py` (sqlite3 → `psycopg`/SQLAlchemy),
   ввести `tenant_ctx` и автоинъекцию `tenant_id` (см. §2). Это самый объёмный
   кусок — делать таблица-за-таблицей, сверяя поведение на tenant 1.
6. **Уникальности → пер-тенантные:** напр. `UNIQUE(chat_id)` → `UNIQUE(tenant_id, chat_id)`.
7. **Катовер:** tenant 1 продолжает работать как сейчас; только потом включаем
   онбординг новых салонов (Этап 1).

**Критерий готовности Этапа 0:** текущий салон полностью работает на Postgres
как `tenant_id=1`, RLS включён, `get_provider()` отдаёт YClients — и при этом
схема уже готова принять `tenant_id=2`.

---

## 4. Риски / на что заложиться

- 🔴 **RLS — обязателен.** Мультитенантные ПД + один баг с забытым `WHERE` = утечка
  между салонами. RLS закрывает это на уровне БД.
- **Расход Claude/OpenAI растёт с тенантами.** Учёт уже есть (`ai_usage_log`) —
  добавить `tenant_id`, агрегировать, заложить в тариф / поставить лимиты.
- **Шифрование секретов тенанта.** Токены провайдера/бота/ЮKassa — только `BYTEA`,
  тем же Fernet. Ключ — платформенный, не в БД.
- **152-ФЗ: мы становимся «обработчиком» ПД для каждого салона** — нужна оферта
  + поручение на обработку. Данные уже в РФ — это плюс.
- **Фоновые джобы** переписать на цикл по тенантам (иначе сработают только для одного).
- **Коллизия имён:** `subscriptions` = абонементы клиентов; B2B-подписка салона —
  это `tenants.status/plan` + `tenant_invoices`. Не смешивать.

---

## 5. Что дальше (Этап 1, после фундамента)

Онбординг (регистрация → «Подключить YClients» логином/1-кликом → автоподтянулись
мастера/услуги → триал) · общий бот с роутингом по тенанту · white-label PWA по
поддомену · рекуррентный биллинг ЮKassa. Подробности — в обзоре выше по чату.
