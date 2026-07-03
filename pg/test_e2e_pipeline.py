# -*- coding: utf-8 -*-
"""
Сквозной e2e SaaS-конвейера (Шаги 1–4 вместе, а не порознь):

  онбординг → резолв по Host → публичный бренд-конфиг (Шаг 1)
            → гейтинг фич по тарифу (Шаг 3) → бизнес-правила (Шаг 2)
            → жизненный цикл статуса (suspend/cancel)

PG не нужен: единая in-memory фейк-БД обслуживает РЕАЛЬНЫЕ SQL всех модулей
(tenants, tenant_addons, tenant_usage). Fernet-шифрование — настоящее.
Запуск: python3 test_e2e_pipeline.py  (или pytest).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tenant_resolver as tr  # noqa: E402


# ─────────────────── единая фейк-БД для всего конвейера ───────────────────
class FakeDB:
    def __init__(self):
        self.tenants: dict[int, dict] = {}
        self.addons: dict[tuple[int, str], bool] = {}
        self.usage: dict[tuple[int, str, str], int] = {}
        self.next_id = 1

    def q(self, sql, params=()):
        s = re.sub(r"\s+", " ", sql).strip()

        if s.startswith("INSERT INTO tenants"):
            tid = self.next_id; self.next_id += 1
            slug, name, email, phone, tg, _days = params
            self.tenants[tid] = {"id": tid, "slug": slug, "display_name": name,
                                 "status": "trial", "plan": "start", "brand": {},
                                 "settings": {}, "provider": None,
                                 "provider_config": None, "active_master_ids": [],
                                 "owner_email": email, "owner_phone": phone,
                                 "owner_tg_id": tg, "trial_ends_at": "2026-07-17"}
            return [{"id": tid}]

        if s.startswith("SELECT id FROM tenants WHERE slug"):
            slug = params[0]
            for t in self.tenants.values():
                if t["slug"] == slug and t["status"] != "cancelled":
                    return [{"id": t["id"]}]
            return []

        m = re.match(r"SELECT ([a-z_, ]+) FROM tenants WHERE id=%s", s)
        if m:
            t = self.tenants.get(params[0])
            if not t:
                return []
            cols = [c.strip() for c in m.group(1).split(",")]
            return [{c: t[c] for c in cols}]

        if s.startswith("UPDATE tenants SET"):
            tid = params[-1]
            t = self.tenants[tid]
            if "SET plan=" in s:
                t["plan"] = params[0]
            elif "SET brand=" in s:
                t["brand"] = json.loads(params[0])
            elif "SET settings=" in s:
                t["settings"] = json.loads(params[0])
            elif "SET provider='yclients', provider_config=" in s:
                t["provider"], t["provider_config"] = "yclients", params[0]
            elif "SET active_master_ids=" in s:
                t["active_master_ids"] = json.loads(params[0])
            elif (lit := re.search(r"SET status='(\w+)'", s)):
                t["status"] = lit.group(1)        # activate(): статус литералом
            elif "SET status=" in s:
                t["status"] = params[0]
            else:
                raise AssertionError("неизвестный UPDATE: " + s)
            return []

        if s.startswith("INSERT INTO tenant_addons"):
            self.addons[(params[0], params[1])] = True
            return []
        if s.startswith("UPDATE tenant_addons SET active=false"):
            self.addons[(params[0], params[1])] = False
            return []
        if s.startswith("SELECT addon_key FROM tenant_addons"):
            return [{"addon_key": k[1]} for k, v in self.addons.items()
                    if k[0] == params[0] and v]
        if s.startswith("SELECT plan FROM tenants"):
            t = self.tenants.get(params[0])
            return [{"plan": t["plan"]}] if t else []
        if s.startswith("INSERT INTO tenant_usage"):
            key = (params[0], params[1], params[2])
            self.usage[key] = self.usage.get(key, 0) + params[3]
            return []
        if s.startswith("SELECT count FROM tenant_usage"):
            n = self.usage.get((params[0], params[1], params[2]))
            return [{"count": n}] if n is not None else []

        raise AssertionError("фейк-БД не знает SQL: " + s)


DB = FakeDB()
tr._q = DB.q

import tenant_onboarding as ob    # noqa: E402
import tenant_config as tc        # noqa: E402
import tenant_config_api as api   # noqa: E402
import tenant_settings as ts      # noqa: E402
import tenant_features as tf      # noqa: E402
import tenant_gate as tg          # noqa: E402


def test_e2e_full_pipeline():
    os.environ.pop(api.MOCK_ENV, None)
    ts.invalidate()

    # ── 0. Тенант №1 = МЭ (мигрирована как max + ai_chatbot) ──
    me = ob.onboard_salon("malesthetic", "Мужская Эстетика",
                          plan="max", addons=["ai_chatbot"])
    assert me["tenant_id"] == 1

    # ── 1. Онбординг нового салона одним вызовом ──
    passport = ob.onboard_salon(
        "griva", "Барбершоп «Грива»", plan="pro",
        brand={"name": "Барбершоп «Грива»", "city": "Краснодар",
               "address": "ул. Красная, 15", "phone": "+7 (900) 123-45-67"},
        owner_tg_id=111222333,
        yclients={"company_id": 777001, "user_token": "yc-secret-777",
                  "cash_account_id": 1, "cashless_account_id": 2},
        provider=ob.FakeProvider())
    tid = passport["tenant_id"]
    assert tid == 2 and passport["host"] == "griva.app.ru"
    assert passport["catalog"]["masters"] == 2

    # секрет YClients в БД зашифрован, но расшифровывается
    raw = bytes(DB.tenants[tid]["provider_config"])
    assert b"yc-secret-777" not in raw
    assert ob.get_provider_config(tid)["company_id"] == 777001

    # ── 2. Резолв по Host (как это сделает каждый запрос PWA) ──
    assert tr.tenant_for_host("griva.app.ru:443") == tid
    assert tr.tenant_for_host("www.app.ru") is None

    # ── 3. Шаг 1: публичный бренд-конфиг (что получит app-tenant.html) ──
    cfg = api.resolve_payload("griva.app.ru")
    assert cfg["slug"] == "griva" and cfg["active"] is True
    assert cfg["brand"]["name"] == "Барбершоп «Грива»"
    assert cfg["brand"]["city"] == "Краснодар"
    assert "provider_config" not in json.dumps(cfg)  # секреты не утекли

    # ── 4. Шаг 3: гейтинг — pro открывает салонное, доп не куплен ──
    assert tg.check_path(tid, "/api/panel/journal_create") is None
    assert tg.check_path(tid, "/api/panel/broadcast") is None
    denied = tg.check_path(tid, "/api/chat")
    assert denied["need"]["addon"] == "ai_chatbot"
    tf.enable_addon(tid, "ai_chatbot")
    assert tg.check_path(tid, "/api/chat") is None
    fp = tg.features_payload(tid)
    assert fp["plan"] == "pro" and "ai_chatbot" in fp["addons"]

    # ── 5. Шаг 2: бизнес-правила per-tenant, изоляция от МЭ ──
    assert ts.rules_for(tid).CASHBACK_PCT == 5           # дефолт платформы
    ts.set_settings(tid, {"loyalty.cashback_pct": 10,
                          "salary.master_pct": {"101": 0.65}})
    assert ts.rules_for(tid).CASHBACK_PCT == 10
    assert ts.rules_for(tid).salary_percent(101) == 0.65
    assert ts.rules_for(1).CASHBACK_PCT == 5             # МЭ не задета
    assert ts.rules_for(1).salary_percent(1460233) == 0.60

    # у МЭ (max + ai_chatbot) ничего не заблокировано
    for r in ("/api/chat", "/api/panel/journal", "/api/panel/broadcast",
              "/api/panel/salary", "/api/cert/create"):
        assert tg.check_path(1, r) is None, r

    # ── 6. Жизненный цикл: suspend → бренд отдаётся с active=False ──
    ob.set_status(tid, "suspended")
    cfg = api.resolve_payload("griva.app.ru")
    assert cfg is not None and cfg["active"] is False    # лендинг «недоступно»
    ob.activate(tid)
    assert api.resolve_payload("griva.app.ru")["active"] is True
    # cancelled → салон исчезает из резолва
    ob.set_status(tid, "cancelled")
    assert tr.tenant_for_host("griva.app.ru") is None
    assert api.resolve_payload("griva.app.ru") is None


def test_onboard_validates_plan_and_addon():
    try:
        ob.onboard_salon("bad", "Bad", plan="nope")
        assert False
    except ValueError:
        pass
    try:
        ob.onboard_salon("bad2", "Bad2", addons=["no_such_addon"])
        assert False
    except ValueError:
        pass


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_"):
            globals()[name]()
            print(f"✓ {name}")
    print("OK — конвейер Шагов 1–4 работает вместе")
