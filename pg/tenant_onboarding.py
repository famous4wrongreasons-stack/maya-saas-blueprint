"""
SaaS Этап 1, Шаг 6 — онбординг салона.

Жизненный цикл нового салона:
  1. create_tenant(slug, name, owner…)  → строка в tenants (status=trial, триал N дней),
     салон сразу резолвится по поддомену slug.
  2. connect_yclients(tid, company_id, user_token, …) → секреты ЗАШИФРОВАНЫ (Fernet)
     в tenants.provider_config (BYTEA). 🔴 partner-токен — платформенный (в окружении),
     а company_id+user_token — салона.
  3. sync_catalog(tid) → через BookingProvider тянем мастеров/услуги из YClients,
     сохраняем active_master_ids (заодно проверяем, что подключение живое).
  4. activate(tid) → status=active (после оплаты/решения).

Секреты тенанта шифруются ПЛАТФОРМЕННЫМ ключом (env SAAS_SECRET_KEY). В песочнице —
эфемерный ключ в .sandbox_secret.key (НЕ боевой PII-ключ).
"""
from __future__ import annotations
import os, json
from cryptography.fernet import Fernet
import tenant_resolver as tr

_KEY = None


def _fernet() -> Fernet:
    global _KEY
    if _KEY is None:
        _KEY = os.environ.get("SAAS_SECRET_KEY")
    if not _KEY:
        kf = os.path.join(os.path.dirname(__file__), ".sandbox_secret.key")
        if os.path.exists(kf):
            _KEY = open(kf).read().strip()
        else:
            _KEY = Fernet.generate_key().decode()
            open(kf, "w").write(_KEY)
    return Fernet(_KEY.encode())


def _enc(d: dict) -> bytes:
    return _fernet().encrypt(json.dumps(d, ensure_ascii=False).encode())


def _dec(b) -> dict:
    if not b:
        return {}
    return json.loads(_fernet().decrypt(bytes(b)).decode())


# ── 1. Создать салон ───────────────────────────────────────────────────────
def create_tenant(slug: str, display_name: str, owner_email: str = None,
                  owner_phone: str = None, owner_tg_id: int = None,
                  trial_days: int = 14) -> int:
    rows = tr._q(
        "INSERT INTO tenants (slug, display_name, status, owner_email, owner_phone, "
        " owner_tg_id, trial_ends_at) "
        "VALUES (%s, %s, 'trial', %s, %s, %s, now() + make_interval(days => %s)) "
        "RETURNING id",
        (slug.strip().lower(), display_name, owner_email, owner_phone,
         owner_tg_id, int(trial_days)),
    )
    return rows[0]["id"]


# ── 2. Подключить YClients ─────────────────────────────────────────────────
def connect_yclients(tenant_id: int, company_id: int, user_token: str,
                     cash_account_id: int = None, cashless_account_id: int = None) -> None:
    cfg = {"company_id": company_id, "user_token": user_token,
           "cash_account_id": cash_account_id, "cashless_account_id": cashless_account_id}
    tr._q("UPDATE tenants SET provider='yclients', provider_config=%s, updated_at=now() "
          "WHERE id=%s", (_enc(cfg), tenant_id))


def get_provider_config(tenant_id: int) -> dict:
    rows = tr._q("SELECT provider, provider_config FROM tenants WHERE id=%s", (tenant_id,))
    if not rows:
        return {}
    return {"provider": rows[0]["provider"], **_dec(rows[0]["provider_config"])}


# ── 3. Подтянуть каталог (мастера/услуги) ──────────────────────────────────
def sync_catalog(tenant_id: int, provider=None) -> dict:
    """provider — для теста можно передать фейковый; в проде берётся get_provider(tenant)."""
    if provider is None:
        from booking_provider import get_provider
        cfg = get_provider_config(tenant_id)
        provider = get_provider({"id": tenant_id, "provider": cfg.get("provider", "yclients"),
                                 "provider_config": cfg})
    masters = provider.get_masters() or []
    services = provider.get_services() or []
    master_ids = [m["id"] for m in masters if isinstance(m, dict) and m.get("id")]
    tr._q("UPDATE tenants SET active_master_ids=%s::jsonb, updated_at=now() WHERE id=%s",
          (json.dumps(master_ids), tenant_id))
    return {"masters": len(masters), "services": len(services), "master_ids": master_ids}


# ── 4. Активировать (после оплаты/триала) ──────────────────────────────────
def activate(tenant_id: int) -> None:
    tr._q("UPDATE tenants SET status='active', updated_at=now() WHERE id=%s", (tenant_id,))


def set_status(tenant_id: int, status: str) -> None:
    tr._q("UPDATE tenants SET status=%s, updated_at=now() WHERE id=%s", (status, tenant_id))


def set_plan(tenant_id: int, plan: str) -> None:
    """Сменить базовый тариф салона (валидируется по plan_catalog)."""
    import plan_catalog as pc
    if plan not in pc.PLANS:
        raise ValueError(f"Неизвестный тариф: {plan!r}")
    tr._q("UPDATE tenants SET plan=%s, updated_at=now() WHERE id=%s", (plan, tenant_id))


# ── 5. Полное заведение салона одним вызовом ────────────────────────────────
def onboard_salon(slug: str, display_name: str, *, plan: str = "start",
                  addons: list[str] | None = None, brand: dict | None = None,
                  owner_email: str = None, owner_phone: str = None,
                  owner_tg_id: int = None, trial_days: int = 14,
                  yclients: dict | None = None, provider=None) -> dict:
    """«Звонок продаж закончился — салон живёт»: создаёт тенанта, ставит тариф,
    включает допы, сохраняет бренд (Шаг 1), опционально подключает YClients и
    тянет каталог. Возвращает паспорт салона для onboarding-письма.

    yclients = {company_id, user_token[, cash_account_id, cashless_account_id]}
    provider — фейк для тестов; в проде берётся из booking_provider по конфигу.
    """
    tid = create_tenant(slug, display_name, owner_email=owner_email,
                        owner_phone=owner_phone, owner_tg_id=owner_tg_id,
                        trial_days=trial_days)
    set_plan(tid, plan)
    if addons:
        import tenant_features as tf
        for a in addons:
            tf.enable_addon(tid, a)
    if brand:
        import tenant_config as tc
        tc.set_branding(tid, **brand)
    catalog = None
    if yclients:
        connect_yclients(tid, **yclients)
        catalog = sync_catalog(tid, provider=provider)
    return {"tenant_id": tid, "slug": slug.strip().lower(), "plan": plan,
            "addons": sorted(addons or []), "trial_days": trial_days,
            "host": f"{slug.strip().lower()}.app.ru", "catalog": catalog}


# Фейковый провайдер для тестов онбординга (без живого YClients).
class FakeProvider:
    def get_masters(self):
        return [{"id": 101, "name": "Илья"}, {"id": 102, "name": "Стас"}]
    def get_services(self):
        return [{"id": 1, "title": "Стрижка", "price_min": 2000},
                {"id": 2, "title": "Борода", "price_min": 800}]
