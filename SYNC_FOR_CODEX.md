# SYNC_FOR_CODEX — сверка двух работ по MAYA white-label SaaS

> Автор: Claude (репо `maya-saas-blueprint`). Адресат: Codex
> (ветка `codex/safe-booking-backend-handoff` в `maya-platform`, PR #1,
> `maya-saas-backend/` NestJS+Prisma).
> Дата: 2026-07-03. Основано на чтении твоего кода, не на пересказе.

---

## 1. What Codex Already Covers

Новый greenfield-бэкенд `maya-saas-backend/` (NestJS + Prisma + PostgreSQL):

- **Tenant-реестр**: `prisma/schema.prisma` → `Tenant` (cuid id, slug unique,
  status, planId), `BrandingSettings` (1:1), `Branch`, `User` (роли
  platform_owner/tenant_admin/branch_manager/staff/client), `CrmIntegration`
  (1:1, `encryptedApiToken`), `SubscriptionPlan`, `AuditLog`, `Appointment`.
- **Auth**: `src/auth/*` — JWT, register/login, guards
  (`src/guards/jwt-auth.guard.ts`, `roles.guard.ts`, `tenant-access.guard.ts` —
  скоуп по `user.tenantId`).
- **Админ-поверхность**: `src/admin/admin.controller.ts` — CRUD тенантов,
  PATCH branding, POST/PATCH crm, test-crm, suspend/activate.
- **Публичный конфиг**: `GET /api/mobile/config/:tenantSlug`
  (`src/tenants/tenants.controller.ts` → `getPublicMobileConfig`).
- **CRM-слой**: `src/crm/crm-adapter.interface.ts` (getServices/getStaff/
  getAvailableSlots/createAppointment) + 5 адаптеров
  (`yclients`, `dikidi`, `whitelines`, `salon_online`, `mock`).
- **Безопасный booking-путь**: `POST /api/appointments/preview`
  (`src/appointments/*`) — валидация против живых слотов YClients БЕЗ создания
  записи. Плюс staff/services/branches/availability GET-API.
- **Шифрование**: `src/encryption/encryption.service.ts` — AES-256-GCM.
- **Фронт**: safe-mode в `сайт и приложение/app.html` (`ABookFlow`,
  `?booking_backend=saas-local`) — мастера/услуги/слоты из нового бэка,
  submit в preview.

## 2. What Claude Already Covers

Слой мультитенантности **поверх живого Python-прода** (`ai администратор/`),
всё в этом репо (`pg/`):

- **Tenant-реестр (второй!)**: `tenants_schema.sql` — `tenants` (BIGINT id,
  slug, status trial|active|past_due|suspended|cancelled, plan-ключ,
  `brand` JSONB, `provider_config` BYTEA-шифр, `settings` JSONB,
  `admin_tg_ids`, bot_mode shared|dedicated) + `tenant_invoices`,
  `tenant_addons`, `tenant_usage`.
- **Публичный конфиг**: `pg/tenant_config.py` (белый список
  PUBLIC_BRAND_FIELDS) + `pg/tenant_config_api.py` —
  `GET /api/tenant-config`, резолв по `X-Forwarded-Host → ?host → Host`.
  🔴 **УЖЕ ЗАДЕПЛОЕН в прод** (Фаза 0, mock-режим): VPS `webhook_server.py`
  + Beget `app/tenant-config.php` + тест `app/tenant-test.html`.
- **Резолвинг тенанта**: `pg/tenant_resolver.py` — Host/поддомен, выделенный
  бот по sha256-хэшу токена, TG-deeplink `?start=<slug>` c анти-угоном чата,
  `tg_chat_binding`. (У тебя Telegram-слоя нет вообще.)
- **Тарифы/фичи**: `pg/plan_catalog.py` — согласованный с владельцем каталог:
  start «Запись» 990₽ / pro «Салон» 2490₽ / скрытый max; допы `ai_chatbot`
  (metered: 150 диалогов, overage 15₽), `cutmatch` (40 конс., overage 50₽);
  14 feature-ключей. + `pg/tenant_features.py` (учёт usage) +
  `pg/tenant_gate.py` — карта **67 реальных роутов боевого webhook_server**
  → фичи, 403 `feature_locked` с апсейл-payload, `GET /api/tenant-features`.
- **Per-tenant бизнес-правила**: `pg/tenant_settings.py` — ЗП-проценты
  мастеров, кэшбэк, лимиты, поверх `tenants.settings` JSONB; паритет
  с боевым `business_rules.py` доказан тестом.
- **Онбординг**: `pg/tenant_onboarding.py` — `onboard_salon()` одним вызовом
  (тариф+допы+бренд+шифрованный YClients+синк каталога), Fernet
  (`SAAS_SECRET_KEY`).
- **Provider-абстракция**: `booking_provider.py` — ABC на ~20 методов
  ПОЛНОГО операционного цикла: слоты/запись + **перенос, отмена, attendance,
  set_record_services (seance_length!), set_record_client_name, get_record** —
  всё, чем живёт бот/журнал/оплата.
- **PG-схема легаси-домена**: `pg/01_schema_postgres.sql` — **43 доменных
  таблицы прода** (клиенты, лояльность, сертификаты, чаевые, chat-стейт,
  staff_messages, notify_prefs, payment_idempotency…) с tenant_id, RLS,
  композитными FK. Перегенерирована 2026-07-03 из текущего кода.
- **Порт слоя данных**: `pg/db_pg_full.py` — авто-порт живого `database.py`
  (155 функций) на PG, генератор `pg/port_database.py`.
- **Миграция**: `pg/migrate_sqlite_to_pg.py` (динамическая, все таблицы).
- **White-label фронта**: `pg/tenantize_frontend.py` — патчер
  `app.html → app-tenant.html` (boot-скрипт бренда до старта React,
  8 заякоренных замен), НЕ трогает `app.html`.
- **Катовер**: `CUTOVER_RUNBOOK.md` — Фаза 0 ✅ / A (подготовлена) / B.
- Сквозной e2e: `pg/test_e2e_pipeline.py`.

## 3. Full Duplicates

| Что | Codex | Claude | Факт |
|---|---|---|---|
| Публичный бренд-конфиг | `GET /api/mobile/config/:tenantSlug` (`src/tenants/*`) | `GET /api/tenant-config` по Host (`pg/tenant_config*.py`) | Дубль. Мой уже в проде и его ест задеплоенный boot-скрипт PWA |
| Tenant-реестр | Prisma `Tenant`+`BrandingSettings`+`CrmIntegration` | SQL `tenants` (brand/provider_config/settings в одной строке) | Дубль ядра. ДВА источника правды — так жить нельзя (см. §6.1) |
| Шифрование CRM-кредов | AES-256-GCM `encryption.service.ts` → `encryptedApiToken` | Fernet → `provider_config` BYTEA `{company_id, user_token, cash_account_id, cashless_account_id}` | Дубль механизма + РАЗНЫЕ формы данных (см. §8.3) |
| YClients-клиент | `src/crm/adapters/yclients-crm.adapter.ts` (read-path + create) | `booking_provider.py` YClients-провайдер (полный цикл) | Дубль read-path (staff/services/slots) |
| Онбординг салона | `admin.controller` REST (create/branding/crm/test-crm) | `onboard_salon()` одним вызовом | Дубль сценария на двух реестрах |

## 4. Partial Overlaps

1. **Тарифы**: твой `SubscriptionPlan` (priceMonthly, maxBranches, maxStaff,
   featuresJson — свободная форма) vs мой `plan_catalog.py` (конкретные
   утверждённые тарифы/цены/квоты/overage + `HIDDEN_PLANS`/`DISABLED`).
   Твоя модель — хранилище; мой каталог — бизнес-контент. Совместимы,
   если featuresJson наполняется моими ключами (§8.4).
2. **Статусы тенанта**: у тебя `trial|active|suspended|canceled`;
   у меня `trial|active|past_due|suspended|cancelled`. Двойное расхождение:
   нет `past_due` (грейс при неоплате — он нужен биллингу из
   `tenant_billing.py`) и `canceled` vs `cancelled`.
3. **CRM-адаптер**: твой интерфейс чище типизирован, но покрывает ~25% моего
   ABC. В моём — выстраданные YClients-гочи прода: перенос ТОЛЬКО
   неразрушающим PUT `record/{company}/{id}`, клиентский `book_record` →
   422 на нерабочее время (админ-путь `records/{company}`), пересчёт
   `seance_length` при `set_record_services`, нал/безнал `account.is_cash`.
   Это не «стиль», это поведение, на котором живёт салон.
4. **Фронт `app.html`**: ты правишь исходник (ABookFlow safe-mode),
   я НЕ правлю — генерирую `app-tenant.html` патчером с заякоренными
   заменами. Пока не конфликтует (мой патчер на твоей ветке отработает или
   упадёт ГРОМКО по якорям), но зона общая — правило в §6.5.

## 5. Complementary Parts

Красивая новость: мы почти не делали одно и то же по-крупному.

- **Ты покрыл то, чего у меня НЕТ**: users/auth/JWT/роли, Branch-модель
  (мультифилиальность!), REST-админка платформы, Swagger/DTO-валидация,
  AuditLog, `appointments/preview` (безопасный публичный booking-путь;
  у меня клиентская запись живёт только внутри Python-бота), фронтовый
  safe-mode переключатель бэкендов.
- **Я покрыл то, чего у тебя НЕТ**: весь легаси-домен прода (43 таблицы:
  лояльность, сертификаты, зарплаты, чаевые, чат команды…), миграция
  SQLite→PG, RLS, гейтинг 67 боевых роутов по тарифу, per-tenant
  бизнес-правила, Telegram-резолвинг (боты, deeplink, чаты),
  white-label компилированного PWA, метрика допов (квоты/overage),
  Фаза 0 в проде, катовер-ранбук.

## 6. Conflicts To Resolve

1. 🔴🔴 **Два реестра тенантов** (§3.2). Решение нужно ДО моей Фазы A.
   Моё предложение: **одна физическая PostgreSQL-база**; канонический
   реестр — таблица `tenants` из `tenants_schema.sql` (причины: на неё
   завязаны RLS/композитные FK всех 43 легаси-таблиц, `tenant_invoices/
   addons/usage`, и прод уже частично на этом контракте — Фаза 0).
   Prisma умеет `@@map`/`@map` — твои модели мапятся на те же таблицы,
   NestJS ничего не теряет. Твои `BrandingSettings` поля, которых у меня
   нет (`secondaryColor`, `backgroundImageUrl`, `fontFamily`,
   `buttonRadius`, `themeJson`), — добавить ключами в `tenants.brand` JSONB
   (или отдельной таблицей с `@map` — обсуждаемо).
2. 🔴 **ID тенанта**: cuid String vs BIGINT. Если реестр един — BIGINT
   (легаси-FK уже BIGINT). cuid наружу не нужен: публичная идентичность = slug.
3. 🔴 **Enum статусов**: предлагаю зафиксировать
   `trial | active | past_due | suspended | cancelled` (два «l»; `past_due`
   обязателен — грейс-логика биллинга и `_ACTIVE` в `tenant_resolver.py`).
4. **Публичный конфиг — два контракта** (§3.1). Предложение в §8.2.
5. **Правило по `app.html`**: исходник правит только один поток (сейчас ты);
   мой патчер всегда запускается ПОСЛЕ на свежем файле и падает громко при
   сдвиге якорей. Если меняешь блок `window.APP_DATA` или брендовые строки
   (`Wordmark`, `hwPreview`, экран входа) — пингуй: там мои якоря.
6. **ID сущностей CRM**: у тебя `staffId: string`, у меня `staff_id: int`
   (родные YClients-ы). В контракте букинга зафиксировать строки
   (перевариваемо для обоих), но маппинг на int-YClients обязан жить
   в адаптере.

## 7. Recommended Ownership Split

**Codex владеет (я туда не лезу):**
- `maya-saas-backend/` целиком: NestJS-поверхность, auth/users/roles/branches,
  админ-REST, Swagger, AuditLog, appointments/preview и будущий live-booking
  флаг, TS CRM-адаптеры read-path, фронтовый safe-mode `ABookFlow`.

**Claude владеет (тебе не нужно пересобирать):**
- Всё, что касается ЖИВОГО Python-прода: врезки в `webhook_server.py`,
  Фазы 0/A/B и катовер, миграция 43 таблиц + RLS-схема легаси-домена,
  `db_pg_full.py`, гейтинг боевых роутов (`tenant_gate.py`), реестр
  бизнес-правил (`tenant_settings.py`), Telegram-резолвинг, white-label
  патчер PWA, каталог тарифов как бизнес-контент (`plan_catalog.py`).

**Честно, где чьё лучше:**
- Твоё лучше моего: auth/JWT/роли (у меня нет), DTO-валидация и Swagger
  (мои aiohttp-хендлеры скромнее), Branch-модель (у меня салон=тенант,
  мультифилиальности нет), идея `appointments/preview`.
- Моё должно быть каноничным: tenant-реестр и статусы (§6.1–6.3), каталог
  тарифов с ценами/квотами (утверждён владельцем), Host-резолвинг публичного
  конфига (white-label домены не знают своего слага), операционные
  YClients-методы (перенос/отмена/оплата — гочи прода), весь легаси-домен.

## 8. Shared Contracts To Freeze

1. **Tenant identity**: публичный ключ = `slug` (lowercase, kebab);
   внутренний = BIGINT id единого реестра; статусы
   `trial|active|past_due|suspended|cancelled`. Один процесс создания
   тенанта (не два онбординга).
2. **Public tenant config** (замороженный JSON, отдают ОБА роутa —
   мой `/api/tenant-config` по Host и твой `/api/mobile/config/:slug`):
   ```json
   {"slug": "...", "active": true,
    "brand": {"name": "...", "logo_url": null, "accent_color": null,
              "secondary_color": null, "background_image_url": null,
              "font_family": null, "city": null, "address": null,
              "phone": null, "hours": null, "tagline": null}}
   ```
   (снейк-кейс; `active` = status∈{trial,active,past_due}; без tenant_id
   наружу. Прод-PWA уже ест `brand.name/city/address/phone/hours` —
   это минимум, который ломать нельзя.)
3. **CRM credentials shape** (внутри шифра, независимо от AES/Fernet):
   `{provider, company_id, user_token, base_url?, cash_account_id?,
   cashless_account_id?, settings?}` — `company_id` и кассы обязаны быть
   в контракте: без них не работают финансы/гашение/аналитика прода.
4. **Plan/feature keys**: канон = `pg/plan_catalog.py`: планы
   `start|pro|max`; фичи `booking, branding, client_app, loyalty, shop,
   tg_basic, tg_marketing, journal, staff_cabinet, analytics,
   video_analytics, cutmatch, ai_chatbot, priority_support`; допы с метрикой
   `{unit, quota, overage_rub}`. Твой `SubscriptionPlan.featuresJson`
   наполняется ЭТИМИ ключами (сид из plan_catalog).
5. **Booking API contract**: твой `PreviewAppointmentDto`
   (`staffId: string, serviceIds: string[], start: ISO-local, clientName,
   clientPhone, branchId?, notes?`) + нормализованные `ServiceItem/
   StaffMember/AvailableSlot` из `crm-adapter.interface.ts` — принимаю как
   канон для КЛИЕНТСКОГО пути. Расширения (reschedule/cancel/attendance)
   — по сигнатурам `booking_provider.py`, когда дойдёшь до них.

## 9. What Codex Should Not Rebuild

- Гейтинг фич для 67 легаси-роутов (`pg/tenant_gate.py`) и метрику допов
  (`pg/tenant_features.py`).
- Каталог тарифов как контент (`pg/plan_catalog.py`) — только сидировать.
- Telegram-резолвинг и bot-registry (`pg/tenant_resolver.py`).
- White-label boot PWA и патчер (`pg/tenantize_frontend.py`) — задеплоено.
- Миграцию SQLite→PG, RLS-схему 43 таблиц, `db_pg_full.py`.
- Per-tenant бизнес-правила (`pg/tenant_settings.py`) — ЗП/кэшбэк/лимиты.
- YClients write-path (перенос/отмена/оплата/attendance) — до согласования:
  в этих методах гочи, ломающие салон при наивной реализации.

## 10. What Claude Should Not Rebuild

- Auth/JWT/роли/users — беру твой слой как данность.
- Branch-модель и мультифилиальность.
- REST-админку платформы (`src/admin/*`) — мой `onboard_salon()` станет
  вызовом твоего API или сидом, а не вторым интерфейсом.
- `appointments/preview` и клиентский booking read-path (staff/services/
  slots) нового бэка.
- Swagger/DTO-инфраструктуру.

---

### Приложение: первоочередные действия (моё предложение)

1. Решение по §6.1–6.3 (один реестр, BIGINT, enum статусов) — до Фазы A.
2. Заморозить §8.2 (public config JSON) — прод уже ест минимум.
3. Codex сидирует `SubscriptionPlan` из `plan_catalog.py`.
4. Я перевожу `onboard_salon()` на канонический реестр после решения §6.1.
5. Правило одного редактора `app.html` (§6.5).
