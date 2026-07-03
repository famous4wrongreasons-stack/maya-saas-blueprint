"""
SaaS: гейтинг фич по тарифу + допам конкретного салона.

Единая точка «доступна ли фича этому салону прямо сейчас». Базовый тариф —
tenants.plan; подключённые допы — tenant_addons. Поверх чистой логики plan_catalog.

Использование в бэке/роутах (после резолва салона в tenant_scope):
    import tenant_features as tf
    if not tf.has_feature(tenant_id, "cutmatch"):
        return _err("Модуль CutMatch не подключён в вашем тарифе")
"""
from __future__ import annotations
from datetime import datetime
import tenant_resolver as tr
import plan_catalog as pc


def tenant_plan(tenant_id: int) -> str:
    r = tr._q("SELECT plan FROM tenants WHERE id=%s", (tenant_id,))
    return r[0]["plan"] if r else "start"


def tenant_addons(tenant_id: int) -> list[str]:
    rows = tr._q("SELECT addon_key FROM tenant_addons WHERE tenant_id=%s AND active",
                 (tenant_id,))
    return [r["addon_key"] for r in rows]


def features_for_tenant(tenant_id: int) -> set[str]:
    return pc.features_for(tenant_plan(tenant_id), tenant_addons(tenant_id))


def has_feature(tenant_id: int, key: str) -> bool:
    return pc.has_feature(tenant_plan(tenant_id), tenant_addons(tenant_id), key)


def monthly_price(tenant_id: int) -> int:
    return pc.monthly_price(tenant_plan(tenant_id), tenant_addons(tenant_id))


def enable_addon(tenant_id: int, addon_key: str) -> None:
    if addon_key not in pc.ADDONS:
        raise ValueError(f"Неизвестный доп: {addon_key}")
    tr._q("INSERT INTO tenant_addons (tenant_id, addon_key, active) VALUES (%s,%s,true) "
          "ON CONFLICT (tenant_id, addon_key) DO UPDATE SET active=true, enabled_at=now()",
          (tenant_id, addon_key))


def disable_addon(tenant_id: int, addon_key: str) -> None:
    tr._q("UPDATE tenant_addons SET active=false WHERE tenant_id=%s AND addon_key=%s",
          (tenant_id, addon_key))


# ── метрика использования (CutMatch и др. метрик-допы) ───────────────────────

def _ym(now=None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


def record_usage(tenant_id: int, feature: str, n: int = 1, now=None) -> None:
    """Засчитать использование метрик-фичи в текущем месяце (атомарный +n)."""
    tr._q(
        "INSERT INTO tenant_usage (tenant_id, feature, ym, count) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (tenant_id, feature, ym) DO UPDATE SET count = tenant_usage.count + EXCLUDED.count",
        (tenant_id, feature, _ym(now), n),
    )


def used_this_month(tenant_id: int, feature: str, now=None) -> int:
    r = tr._q("SELECT count FROM tenant_usage WHERE tenant_id=%s AND feature=%s AND ym=%s",
              (tenant_id, feature, _ym(now)))
    return r[0]["count"] if r else 0


def metered_status(tenant_id: int, addon_key: str, now=None) -> dict:
    """Состояние метрики допа: включено/квота/использовано/остаток/в сверхлимите.
    addon_key совпадает с feature-ключом (cutmatch, ai_chatbot)."""
    enabled = has_feature(tenant_id, addon_key)
    m = pc.metered_allowance(addon_key) or {}
    used = used_this_month(tenant_id, addon_key, now) if enabled else 0
    quota = int(m.get("quota", 0))
    return {
        "addon": addon_key,
        "unit": m.get("unit"),
        "enabled": enabled,
        "quota": quota,
        "used": used,
        "remaining": max(0, quota - used),
        "in_overage": enabled and used >= quota,
        "overage_rub": int(m.get("overage_rub", 0)),
        "quality": m.get("quality"),
    }


def allowed(tenant_id: int, addon_key: str) -> bool:
    """Можно ли салону пользоваться метрик-фичей. С метрикой жёсткой блокировки нет:
    сверх квоты тарифицируется (overage). Нужна лишь включённая фича."""
    return has_feature(tenant_id, addon_key)


def cutmatch_status(tenant_id: int, now=None) -> dict:
    """Обёртка над metered_status для CutMatch (обратная совместимость)."""
    return metered_status(tenant_id, "cutmatch", now)


def cutmatch_allowed(tenant_id: int, now=None) -> bool:
    return allowed(tenant_id, "cutmatch")
