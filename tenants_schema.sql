-- =====================================================================
-- SaaS Этап 0 — схема мультитенантности (PostgreSQL)
-- Цель: один код обслуживает много салонов. Текущий салон = tenant_id 1.
--
-- ВАЖНО про имена:
--   * tenants            — САЛОНЫ-клиенты платформы (арендаторы).
--   * tenant_invoices    — подписка САЛОНА на нашу платформу (биллинг B2B).
--   * табл. subscriptions (НЕ здесь) — это абонементы КЛИЕНТОВ салона. Не путать!
--
-- Секреты (токены провайдера, бота, ЮKassa) храним ЗАШИФРОВАННЫМИ (BYTEA),
-- тем же Fernet-ключом, что и ПД (см. anonymizer/PII_ENCRYPTION_KEY).
-- Платформенные ключи (наш partner-токен YClients, ключи Claude/OpenAI,
-- PROXY_URL) остаются в окружении сервиса — это НЕ пер-тенантные данные.
-- =====================================================================

-- ──────────────────────────────────────────────────────────────────
-- 1. Реестр тенантов
-- ──────────────────────────────────────────────────────────────────
CREATE TABLE tenants (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug               TEXT UNIQUE NOT NULL,          -- поддомен: <slug>.app.ru
    display_name       TEXT NOT NULL,                 -- «Мужская Эстетика»
    status             TEXT NOT NULL DEFAULT 'trial', -- trial|active|past_due|suspended|cancelled
    plan               TEXT NOT NULL DEFAULT 'start', -- start|pro|...

    -- Бренд (бывшие BARBERSHOP_* / SITE_URL / APP_URL)
    brand              JSONB NOT NULL DEFAULT '{}',
    -- {name,address,city,phone,hours,gis_url,yandex_url,site_url,app_url,logo_url,accent_color}

    -- Платформа записи
    provider           TEXT NOT NULL DEFAULT 'yclients', -- yclients|altegio|dikidi|...
    provider_config    BYTEA,   -- ШИФР: {company_id,user_token,cash_account_id,cashless_account_id}
    active_master_ids  JSONB NOT NULL DEFAULT '[]',

    -- Telegram-бот
    bot_mode           TEXT NOT NULL DEFAULT 'shared',  -- shared (общий бот+deeplink) | dedicated
    bot_config         BYTEA,   -- ШИФР: {token,username} — только для dedicated
    admin_tg_ids       JSONB NOT NULL DEFAULT '[]',     -- бывший INITIAL_ADMIN_IDS

    -- Платежи салона СВОИМ клиентам (их ЮKassa для сертификатов/абонементов) — опционально
    payments_config    BYTEA,   -- ШИФР: {yukassa_shop_id,yukassa_secret_key}

    -- Прочие настройки (бывшие REMINDER_MINUTES_BEFORE, PII_RETENTION_MONTHS, VK_*, SMSRU_*)
    settings           JSONB NOT NULL DEFAULT '{}',
    locale             TEXT NOT NULL DEFAULT 'ru',
    timezone           TEXT NOT NULL DEFAULT 'Europe/Moscow',

    -- B2B-подписка салона на платформу (НЕ путать с табл. subscriptions = абонементы клиентов)
    trial_ends_at      TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,                  -- до какой даты оплачено
    billing_method_id  TEXT,                         -- сохранённый payment_method в ЮKassa (автосписание)

    -- Владелец / контакт
    owner_email        TEXT,
    owner_phone        TEXT,
    owner_tg_id        BIGINT,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenants_status ON tenants(status);

-- ──────────────────────────────────────────────────────────────────
-- 2. Биллинг B2B: история начислений/списаний за подписку на платформу
-- ──────────────────────────────────────────────────────────────────
CREATE TABLE tenant_invoices (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    plan          TEXT NOT NULL,
    amount_rub    INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|paid|failed|refunded
    yukassa_payment_id TEXT,
    paid_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tenant_invoices_tenant ON tenant_invoices(tenant_id, period_start);

-- Опционально: метрика расхода AI на тенанта (для маржи/лимитов тарифа).
-- Уже есть ai_usage_log — просто добавить туда tenant_id (см. ниже) и агрегировать.

-- ──────────────────────────────────────────────────────────────────
-- 3. tenant_id во ВСЕ доменные таблицы (31 шт.)
--    Паттерн одинаковый. Пример на clients; повторить для каждой.
-- ──────────────────────────────────────────────────────────────────
-- Список таблиц, которым нужен tenant_id:
--   admins, ai_advice_log, ai_usage_log, birthday_promo, bookings,
--   client_chat_state, client_history_cache, clients, consents,
--   cutmatch_usage, cycle_reminder_log, freed_slot_offers, gift_certificates,
--   loyalty_redeem_codes, loyalty_transactions, masters_telegram,
--   processed_records, reactivation_log, record_state, referral_codes,
--   referral_promos, referrals, review_requests, salon_expenses, settings,
--   slot_waitlist, stylist_consents, subscriptions, tips,
--   web_login_codes, web_sessions

-- Пример (clients):
ALTER TABLE clients ADD COLUMN tenant_id BIGINT NOT NULL DEFAULT 1
    REFERENCES tenants(id);
ALTER TABLE clients ALTER COLUMN tenant_id DROP DEFAULT;   -- после бэкфилла
CREATE INDEX idx_clients_tenant ON clients(tenant_id);
-- Уникальности тоже становятся пер-тенантными:
--   было UNIQUE(chat_id) → стало UNIQUE(tenant_id, chat_id)

-- Генератор ALTER-ов для всех таблиц разом (выполнить в psql):
DO $$
DECLARE t TEXT;
BEGIN
  FOR t IN SELECT unnest(ARRAY[
    'admins','ai_advice_log','ai_usage_log','birthday_promo','bookings',
    'client_chat_state','client_history_cache','clients','consents',
    'cutmatch_usage','cycle_reminder_log','freed_slot_offers','gift_certificates',
    'loyalty_redeem_codes','loyalty_transactions','masters_telegram',
    'processed_records','reactivation_log','record_state','referral_codes',
    'referral_promos','referrals','review_requests','salon_expenses','settings',
    'slot_waitlist','stylist_consents','subscriptions','tips',
    'web_login_codes','web_sessions'])
  LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);', t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_tenant ON %I(tenant_id);', t, t);
  END LOOP;
END $$;
-- После бэкфилла (всё = tenant 1) снять DEFAULT:
-- ... ALTER COLUMN tenant_id DROP DEFAULT;  (по желанию — DEFAULT 1 безопасен на переходный период)

-- ──────────────────────────────────────────────────────────────────
-- 4. Row-Level Security — страховка от утечки между тенантами на уровне БД
--    Даже если в коде забыли WHERE tenant_id=..., Postgres не отдаст чужое.
--    Приложение в начале запроса/транзакции делает:  SET app.tenant_id = '<id>';
-- ──────────────────────────────────────────────────────────────────
-- Пример (clients), повторить для всех доменных таблиц:
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clients
    USING (tenant_id = current_setting('app.tenant_id', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint);

-- Платформенные/служебные запросы (миграции, кросс-тенантная аналитика для нас)
-- ходят под ролью с BYPASSRLS или явно SET app.tenant_id для нужного салона.
