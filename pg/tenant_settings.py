# -*- coding: utf-8 -*-
"""
SaaS Шаг 2 — бизнес-константы бэкенда → per-tenant настройки.

Хранение: существующая колонка tenants.settings JSONB (см. tenants_schema.sql).
Здесь НЕТ секретов и ПД: токены/кассы/company_id живут в шифрованном
provider_config, а бренд — в tenants.brand (Шаг 1). Здесь только числа-правила.

Дефолты каждого ключа = СЕГОДНЯШНИЕ боевые значения «Мужской Эстетики»,
поэтому tenant 1 без единой настройки ведёт себя ровно как прод сейчас
(это проверяет тест паритета с business_rules.py).

Подключение к боевому коду потом — минимальный дифф через legacy-обёртку:

    rules = tenant_settings.rules_for(tenant_id)
    rules.CASHBACK_PCT                # было: loyalty.CASHBACK_PCT
    rules.CUTMATCH_DAILY_LIMIT        # было: webhook_server.CUTMATCH_DAILY_LIMIT
    rules.REMINDER_MINUTES_BEFORE     # было: config.REMINDER_MINUTES_BEFORE
    rules.salary_percent(staff_id)    # было: business_rules.salary_percent

Карта миграции (legacy → ключ настройки):
    business_rules.MASTER_SALARY_PCT      → salary.master_pct          {staff_id: доля}
    business_rules.MASTER_SALARY_DEFAULT  → salary.master_default
    business_rules.OWNER_STAFF_ID         → salary.owner_staff_id
    loyalty.CASHBACK_PCT                  → loyalty.cashback_pct
    webhook_server.CUTMATCH_DAILY_LIMIT   → limits.cutmatch_daily
    config.GOD_AI_BUDGET_USD              → limits.ai_budget_usd
    config.REMINDER_MINUTES_BEFORE        → notify.reminder_minutes_before
    config.PII_RETENTION_MONTHS           → privacy.pii_retention_months

⚠ Гоча прода: MASTER_SALARY_PCT/DEFAULT продублированы в webhook_server.py
(~L3160) мимо business_rules.py — при подключении Шага 2 дубль убрать,
иначе дрейф значений.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import tenant_resolver as tr

# ─────────────────────────── реестр настроек ───────────────────────────
# ключ → (дефолт МЭ, нормализатор/валидатор, описание)
# Нормализатор бросает ValueError на мусор и приводит тип (JSON-ключи словарей
# приходят строками → приводим staff_id к int).


def _pct01(v) -> float:
    f = float(v)
    if not 0.0 <= f <= 1.0:
        raise ValueError("доля должна быть в диапазоне 0..1")
    return f


def _pct_map(v) -> dict[int, float]:
    if not isinstance(v, dict):
        raise ValueError("ожидается словарь {staff_id: доля}")
    return {int(k): _pct01(x) for k, x in v.items()}


def _int_pos(v) -> int:
    i = int(v)
    if i < 0:
        raise ValueError("значение не может быть отрицательным")
    return i


def _int_id(v) -> int:
    i = int(v)
    if i <= 0:
        raise ValueError("ожидается положительный id")
    return i


def _money(v) -> float:
    f = float(v)
    if f < 0:
        raise ValueError("сумма не может быть отрицательной")
    return f


def _cashback(v) -> int:
    i = int(v)
    if not 0 <= i <= 50:
        raise ValueError("кэшбэк 0..50%")
    return i


SPEC: dict[str, tuple[Any, Callable[[Any], Any], str]] = {
    # зарплаты (см. business_rules.py — единый источник в проде сегодня)
    "salary.master_pct":     ({1460233: 0.60}, _pct_map,
                              "персональные доли мастеров {staff_id: 0..1}"),
    "salary.master_default": (0.50, _pct01, "доля мастера по умолчанию"),
    "salary.owner_staff_id": (1461615, _int_id,
                              "staff_id владельца (его доля всегда 1.0)"),
    # лояльность
    "loyalty.cashback_pct":  (5, _cashback, "кэшбэк баллами, % с визита"),
    # лимиты
    "limits.cutmatch_daily": (2, _int_pos, "CutMatch-консультаций в день на клиента"),
    "limits.ai_budget_usd":  (50.0, _money, "месячный ИИ-бюджет салона, USD"),
    # уведомления
    "notify.reminder_minutes_before": (120, _int_pos,
                                       "напоминание клиенту, минут до визита"),
    # приватность (152-ФЗ)
    "privacy.pii_retention_months": (18, _int_pos, "ретенция ПД, месяцев"),
}


def defaults() -> dict[str, Any]:
    """Дефолты платформы = сегодняшнее поведение «Мужской Эстетики»."""
    return {k: v for k, (v, _n, _d) in SPEC.items()}


# ─────────────────────────── хранение (tenants.settings) ───────────────────────────

def _load_raw(tenant_id: int) -> dict:
    rows = tr._q("SELECT settings FROM tenants WHERE id=%s", (tenant_id,))
    if not rows:
        raise LookupError(f"tenant {tenant_id} не найден")
    return rows[0]["settings"] or {}


def _store_raw(tenant_id: int, data: dict) -> None:
    tr._q("UPDATE tenants SET settings=%s::jsonb, updated_at=now() WHERE id=%s",
          (json.dumps(data, ensure_ascii=False), tenant_id))


# TTL-кэш, чтобы не ходить в PG на каждый запрос; сбрасывается при записи.
_CACHE: dict[int, tuple[float, dict]] = {}
CACHE_TTL = 60.0


def invalidate(tenant_id: int | None = None) -> None:
    if tenant_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(tenant_id, None)


def get_settings(tenant_id: int) -> dict[str, Any]:
    """Полный словарь настроек тенанта: дефолты ← перекрытия из БД.
    Неизвестные/битые ключи из БД игнорируются (не роняют салон)."""
    hit = _CACHE.get(tenant_id)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return dict(hit[1])
    merged = defaults()
    for k, v in _load_raw(tenant_id).items():
        spec = SPEC.get(k)
        if spec is None:
            continue
        try:
            merged[k] = spec[1](v)
        except (ValueError, TypeError):
            continue  # битое значение → остаётся дефолт
    _CACHE[tenant_id] = (time.monotonic(), dict(merged))
    return merged


def set_settings(tenant_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    """Валидирует и сохраняет перекрытия. Неизвестный ключ/битое значение →
    ValueError ДО записи (ничего не сохраняется частично)."""
    clean: dict[str, Any] = {}
    for k, v in updates.items():
        spec = SPEC.get(k)
        if spec is None:
            raise ValueError(f"неизвестная настройка: {k!r}")
        clean[k] = spec[1](v)
    data = _load_raw(tenant_id)
    data.update(clean)
    _store_raw(tenant_id, data)
    invalidate(tenant_id)
    return get_settings(tenant_id)


# ─────────────────────────── legacy-обёртка для прода ───────────────────────────

class TenantRules:
    """Имена как у сегодняшних констант — чтобы подключение было минимальным диффом."""

    def __init__(self, s: dict[str, Any]):
        self._s = s
        self.MASTER_SALARY_PCT: dict[int, float] = s["salary.master_pct"]
        self.MASTER_SALARY_DEFAULT: float = s["salary.master_default"]
        self.OWNER_STAFF_ID: int = s["salary.owner_staff_id"]
        self.CASHBACK_PCT: int = s["loyalty.cashback_pct"]
        self.CUTMATCH_DAILY_LIMIT: int = s["limits.cutmatch_daily"]
        self.GOD_AI_BUDGET_USD: float = s["limits.ai_budget_usd"]
        self.REMINDER_MINUTES_BEFORE: int = s["notify.reminder_minutes_before"]
        self.PII_RETENTION_MONTHS: int = s["privacy.pii_retention_months"]

    def salary_percent(self, staff_id: int) -> float:
        """Сигнатура и семантика business_rules.salary_percent, но per-tenant."""
        if staff_id == self.OWNER_STAFF_ID:
            return 1.0
        return self.MASTER_SALARY_PCT.get(staff_id, self.MASTER_SALARY_DEFAULT)


def rules_for(tenant_id: int) -> TenantRules:
    return TenantRules(get_settings(tenant_id))
