"""
SaaS Этап 1, Шаг 8 — white-label: бренд салона + ПУБЛИЧНЫЙ конфиг для PWA.

Каждый салон — свой поддомен (slug.app.ru) со своим лого/цветами/названием.
Фронт (precompiled-React index.html) на старте дёргает GET /api/tenant-config,
бэкенд резолвит салон по Host и отдаёт ПУБЛИЧНЫЙ бренд-конфиг.

🔴 БЕЗОПАСНОСТЬ: этот эндпоинт НЕаутентифицированный (лендинг до логина), поэтому
отдаём СТРОГО белый список бренд-полей — никаких токенов/секретов/чужих салонов.
Достигается тем, что provider_config/billing/owner вообще НЕ выбираются из БД,
а brand фильтруется по PUBLIC_BRAND_FIELDS. Числовой tenant_id наружу не отдаём
(фронт его не знает и не должен — бэкенд резолвит салон по Host на каждом запросе).
"""
from __future__ import annotations
import json
import tenant_resolver as tr

PUBLIC_BRAND_FIELDS = (
    "name", "logo_url", "accent_color", "city", "address", "phone",
    "hours", "site_url", "gis_url", "yandex_url", "tagline",
)


def set_branding(tenant_id: int, **brand) -> None:
    """Сохранить/обновить бренд салона (мерж в tenants.brand). None-значения игнорим."""
    cur = tr._q("SELECT brand FROM tenants WHERE id=%s", (tenant_id,))
    cur = (cur[0]["brand"] if cur else None) or {}
    cur.update({k: v for k, v in brand.items() if v is not None})
    tr._q("UPDATE tenants SET brand=%s::jsonb, updated_at=now() WHERE id=%s",
          (json.dumps(cur, ensure_ascii=False), tenant_id))


def public_config(host: str) -> dict | None:
    """Публичный бренд-конфиг салона по поддомену (Host). None, если салон не найден.
    Возвращает ТОЛЬКО безопасные поля — секреты физически не выбираются из БД."""
    tid = tr.tenant_for_host(host)
    if tid is None:
        return None
    rows = tr._q(
        "SELECT slug, display_name, status, brand FROM tenants WHERE id=%s", (tid,)
    )  # ← НЕ выбираем provider_config/billing_method_id/owner_* — не утекут
    if not rows:
        return None
    t = rows[0]
    brand = t["brand"] or {}
    safe = {k: brand[k] for k in PUBLIC_BRAND_FIELDS if brand.get(k) is not None}
    safe.setdefault("name", t["display_name"])
    return {
        "slug": t["slug"],
        "brand": safe,
        # доступен ли сервис (для лендинга: показать «временно недоступно» при блоке).
        "active": t["status"] in ("trial", "active", "past_due"),
    }
