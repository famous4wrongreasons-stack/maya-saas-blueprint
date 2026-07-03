-- ╔══════════════════════════════════════════════════════════════════╗
-- ║  SaaS Шаг 4 — слой маршрутизации «внешняя личность → салон».        ║
-- ║  Эти таблицы ГЛОБАЛЬНЫЕ (без RLS): их читают ДО того, как узнали   ║
-- ║  тенанта, поэтому фильтровать по tenant_id здесь нечем.            ║
-- ╚══════════════════════════════════════════════════════════════════╝

-- Привязка чата ОБЩЕГО бота к салону (shared-bot режим).
-- Когда клиент стартует бота по ссылке салона (?start=salon_<id>),
-- запоминаем chat_id → tenant_id; дальше его сообщения резолвятся по этой строке.
CREATE TABLE IF NOT EXISTS tg_chat_binding (
    chat_id    BIGINT PRIMARY KEY,
    tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bound_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tg_chat_binding_tenant ON tg_chat_binding(tenant_id);

-- Реестр ВЫДЕЛЕННЫХ ботов (white-label): токен бота → салон.
-- Храним SHA-256 хэш токена, а не сам токен (токен — секрет).
CREATE TABLE IF NOT EXISTS bot_registry (
    token_hash TEXT PRIMARY KEY,
    tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    username   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_registry_tenant ON bot_registry(tenant_id);

-- Резолвинг по сайту — по поддомену: tenants.slug уже UNIQUE (см. схему).

-- Права роли приложения на новые таблицы (создаются после прежнего GRANT).
GRANT SELECT, INSERT, UPDATE, DELETE ON tg_chat_binding, bot_registry TO salon_app;
