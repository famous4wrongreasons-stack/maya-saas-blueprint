# -*- coding: utf-8 -*-
"""Тесты tenant_settings: паритет с продом, roundtrip, валидация, кэш.
Запуск: python3 test_tenant_settings.py  (или pytest). PG не нужен —
хранилище подменяется in-memory фейком tr._q."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.abspath(os.path.join(HERE, "..", ".."))  # ai администратор/
sys.path.insert(0, HERE)
sys.path.insert(0, PROD)

import tenant_resolver as tr  # noqa: E402
import tenant_settings as ts  # noqa: E402
import business_rules as br   # noqa: E402  (боевой модуль — эталон паритета)


# ── in-memory фейк tenants.settings вместо PG ──
_DB: dict[int, dict] = {1: {}}


def _fake_q(sql, params=()):
    import json as _json
    if sql.startswith("SELECT settings"):
        tid = params[0]
        if tid not in _DB:
            return []
        return [{"settings": _DB[tid]}]
    if sql.startswith("UPDATE tenants SET settings"):
        _DB[params[1]] = _json.loads(params[0])
        return []
    raise AssertionError("неожиданный SQL: " + sql)


tr._q = _fake_q


def _fresh():
    _DB.clear()
    _DB[1] = {}
    _DB[2] = {}
    ts.invalidate()


def test_parity_with_business_rules():
    """Тенант 1 без настроек = сегодняшний прод (business_rules.py)."""
    _fresh()
    rules = ts.rules_for(1)
    assert rules.MASTER_SALARY_PCT == br.MASTER_SALARY_PCT
    assert rules.MASTER_SALARY_DEFAULT == br.MASTER_SALARY_DEFAULT
    assert rules.OWNER_STAFF_ID == br.OWNER_STAFF_ID
    for sid in (br.OWNER_STAFF_ID, 1460233, 1461621, 3278920, 99999):
        assert rules.salary_percent(sid) == br.salary_percent(sid), sid
    # и остальные дефолты = боевые значения
    assert rules.CASHBACK_PCT == 5
    assert rules.CUTMATCH_DAILY_LIMIT == 2
    assert rules.REMINDER_MINUTES_BEFORE == 120
    assert rules.PII_RETENTION_MONTHS == 18


def test_set_get_roundtrip_isolated_per_tenant():
    _fresh()
    ts.set_settings(2, {"loyalty.cashback_pct": 10,
                        "salary.master_default": 0.45,
                        "salary.master_pct": {"777": 0.7}})  # строковый ключ из JSON
    r2 = ts.rules_for(2)
    assert r2.CASHBACK_PCT == 10
    assert r2.salary_percent(777) == 0.7          # ключ приведён к int
    assert r2.salary_percent(555) == 0.45
    # тенант 1 не задет
    r1 = ts.rules_for(1)
    assert r1.CASHBACK_PCT == 5 and r1.salary_percent(555) == 0.50


def test_validation_rejects_garbage_before_write():
    _fresh()
    for bad in ({"loyalty.cashback_pct": 99},
                {"salary.master_default": 1.5},
                {"limits.cutmatch_daily": -1},
                {"no.such.key": 1}):
        try:
            ts.set_settings(1, bad)
            assert False, f"должно было упасть: {bad}"
        except ValueError:
            pass
    assert _DB[1] == {}, "частичная запись запрещена"


def test_broken_db_value_falls_back_to_default():
    _fresh()
    _DB[1] = {"loyalty.cashback_pct": "мусор", "unknown.key": 1,
              "salary.master_default": 0.55}
    ts.invalidate()
    r = ts.rules_for(1)
    assert r.CASHBACK_PCT == 5            # битое → дефолт, не падение
    assert r.MASTER_SALARY_DEFAULT == 0.55  # валидное перекрытие применилось


def test_cache_invalidation_on_write():
    _fresh()
    assert ts.rules_for(1).CASHBACK_PCT == 5
    ts.set_settings(1, {"loyalty.cashback_pct": 7})
    assert ts.rules_for(1).CASHBACK_PCT == 7  # кэш сброшен записью


if __name__ == "__main__":
    for name in sorted(list(globals())):
        if name.startswith("test_"):
            globals()[name]()
            print(f"✓ {name}")
    print("OK")
