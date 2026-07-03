# MAYA · White-Label SaaS — слой мультитенантности (работа Claude)

> Хэндовер-репозиторий для сверки параллельных работ.
> **Кто что делает:** Claude (этот репо) шёл от работающего продукта МЭ —
> интеграционный слой мультитенантности поверх существующего бэкенда
> (`webhook_server.py` aiohttp + сырой SQLite `database.py`) и фронта
> (precompiled-React `app.html`). **Codex** пишет backend white-label SaaS
> отдельно. Задача этого репо — чтобы Codex увидел, что уже сделано/задеплоено,
> и вы не строили одно и то же дважды.

## TL;DR статус

| Слой | Модуль | Статус |
|---|---|---|
| Бренд из БД → PWA | `pg/tenant_config.py` + `pg/tenant_config_api.py` + патчер `pg/tenantize_frontend.py` | ✅ тесты · **✅ ЗАДЕПЛОЕНО в прод (Фаза 0, mock-режим)** |
| Бизнес-правила per-tenant | `pg/tenant_settings.py` (ЗП/кэшбэк/лимиты поверх `tenants.settings` JSONB) | ✅ тесты, паритет с продом доказан |
| Гейтинг фич по тарифу | `pg/plan_catalog.py` + `pg/tenant_features.py` + HTTP-слой `pg/tenant_gate.py` | ✅ тесты (67 реальных роутов прода размечены) |
| Онбординг салона | `pg/tenant_onboarding.py` (`onboard_salon()` одним вызовом) | ✅ тесты + сквозной e2e |
| Резолвинг тенанта | `pg/tenant_resolver.py` (Host/бот-токен/deeplink/чат) | ✅ (аудит-фиксы захвата чата внутри) |
| Схема PG + RLS | `pg/01_schema_postgres.sql` (**регенерирована 2026-07-03: 43 доменных + 4 платформенных таблицы**) | ✅ строгая валидация |
| Порт database.py → PG | `pg/db_pg_full.py` (авто-порт через `pg/port_database.py`, 9/9 правок, 0 `INSERT OR REPLACE`) | ✅ компилируется; ждёт живой PG |
| Миграция данных | `pg/migrate_sqlite_to_pg.py` (динамический — берёт все таблицы из sqlite_master) | ✅ репетировался ранее |
| Биллинг B2B | `pg/tenant_billing.py` + `tenants_schema.sql` (tenant_invoices) | 🟡 черновик, автосписание не подключено |
| Джобы по тенантам | `pg/tenant_jobs.py` | 🟡 каркас |

Тесты запускаются БЕЗ Postgres: `python3 pg/test_tenant_config_api.py`,
`test_tenant_settings.py`, `test_tenant_gate.py`, `test_e2e_pipeline.py`
(остальные test_* писались под локальный репетиционный PG, которого больше нет).

## Что УЖЕ живёт в проде (Фаза 0, задеплоено 2026-07-03)

Прод-приложение МЭ не изменено; добавлены обходные пути:

- **VPS** (`/home/botadmin/barbershop-bot/`): `tenant_config_api.py` + `tenant_me.json`
  (бренд МЭ) + 3 строки в `webhook_server.py` сразу после `web.Application(...)`
  (бэкап `webhook_server.py.phase0.bak`). Эндпоинт: `GET /api/tenant-config`
  (mock-режим через env `MAYA_TENANT_CONFIG_MOCK`, Postgres не требуется).
- **Beget**: `app/tenant-config.php` (новый файл-прокси на `https://rt.malesthetic.pro`;
  общий `api-proxy.php` НЕ тронут) + `app/tenant-test.html` (сгенерирован патчером,
  боевой `app/index.html` не тронут). Тест: https://malesthetic.pro/app/tenant-test.html
- Смока прошла: подмена `tenant_me.json` на демо-салон «Грива» переодевает PWA без
  единой правки кода; откат — вернуть JSON.

⚠ Сантехника, которую надо знать: фронт МЭ **не** ходит на same-origin `/api/*` —
только через PHP-прокси на Beget; прямой `VPS:8080` закрыт фаерволом, боевой путь
Beget→VPS = `https://rt.malesthetic.pro` (nginx, извне 403). Boot-скрипт патчера
пробует цепочку URL и передаёт `?host=`; бэкенд резолвит X-Forwarded-Host → ?host → Host.

## Дорожная карта (CUTOVER_RUNBOOK.md)

- **Фаза 0** ✅ — white-label вживую без PG (см. выше).
- **Фаза A** (следующая) — МЭ на Postgres как tenant_id=1, поведение 1:1.
  Подготовка сделана: схема и порт перегенерированы из ТЕКУЩЕГО кода
  (найден и вылечен дрейф: +12 таблиц, появившихся после написания blueprint;
  +3 новых `INSERT OR REPLACE`, которые PG бы не принял).
  Блокеры: Yandex Managed PG (шаг владельца), `psycopg2-binary`+`postgresql-client`
  на VPS, снапшот боевой SQLite.
- **Фаза B** — включить мультитенантность и продажи. 🔴 В ранбуке чеклист
  «перед салоном №2» (композитные PK для natural-key таблиц).

## Codex: где вероятен дубль — сверь в первую очередь

1. **`GET /api/tenant-config`** (публичный бренд-конфиг по Host) — уже реализован
   и задеплоен. Если у тебя есть аналог — сверить контракт (см. `pg/tenant_config.py`:
   белый список PUBLIC_BRAND_FIELDS, `active`-флаг, без tenant_id наружу).
2. **Резолвинг тенанта** (Host/slug, выделенный бот по хэшу токена, deeplink
   `?start=<slug>`, привязка чата) — `pg/tenant_resolver.py`, включая аудит-фиксы
   (перебор числовых id, угон чата чужой ссылкой).
3. **Тарифы/фичи/допы с метрикой** — `pg/plan_catalog.py` (start/pro/max, допы
   ai_chatbot/cutmatch с квотами и overage) + `pg/tenant_gate.py` (карта 67 роутов
   боевого webhook_server → фичи, 403 `feature_locked` c апсейл-payload).
4. **Реестр per-tenant настроек** — `pg/tenant_settings.py` (валидация до записи,
   TTL-кэш, legacy-обёртка `rules_for()` под имена констант боевого кода).
5. **Онбординг** — `pg/tenant_onboarding.py`: `onboard_salon()` = создать+тариф+допы+
   бренд+шифрованное подключение YClients (Fernet, ключ в env)+синк каталога.
6. **Схема PG** — `pg/01_schema_postgres.sql`: tenant_id во всех 43 таблицах,
   RLS-политики, композитные FK (tenant_id, id). Если у Codex своя схема — решить,
   чья каноническая, ДО Фазы A.
7. **Слой данных** — `pg/db_pg_full.py`: это не новый DAL, а авто-порт живого
   `database.py` (155 функций) с сохранением API. Если Codex пишет новый DAL —
   это самый большой риск дубля.

## Как перегенерировать артефакты (не править руками)

- Схема: дамп sqlite_master в формате `-- [table] имя\nDDL;` → `/tmp/prod_schema.sql`
  → `python3 pg/generate.py` (⚠ без маркеров молча даст 0 таблиц).
- Порт: `python3 pg/port_database.py` (вход — боевой `database.py`; якорь `_db()`
  и список FIXES обновлять при дрейфе — скрипт падает громко).
- Фронт: `python3 pg/tenantize_frontend.py <путь к app.html>` → `app-tenant.html`
  (8 заякоренных замен + boot-скрипт; встроенный `node --check`).
