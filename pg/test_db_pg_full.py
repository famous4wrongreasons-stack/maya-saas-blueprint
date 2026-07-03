"""Широкий смоук-тест db_pg_full.py на живой saas_test (роль salon_app, RLS).
Каждая функция проверяется отдельно — итог: сколько прошло/упало."""
import sys, types
stub = types.ModuleType("pii_crypto")
stub.encrypt = lambda s: f"enc({s})"
stub.decrypt = lambda s: s[4:-1] if s and str(s).startswith("enc(") else s
stub.hash_phone = lambda s: "h:" + str(s)
sys.modules["pii_crypto"] = stub

import db_pg_full as db

PASS = FAIL = 0
def check(label, fn):
    global PASS, FAIL
    try:
        r = fn()
        s = repr(r)
        print(f"  ✓ {label}: {s[:70]}")
        PASS += 1
        return r
    except Exception as e:
        print(f"  ✗ {label}: {type(e).__name__}: {e}")
        FAIL += 1
        return None

db.set_tenant(1)
# реальные id из перенесённых данных
with db._db() as c:
    cid = c.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"]
    rs = c.execute("SELECT record_id FROM record_state LIMIT 1").fetchone()
    rec_id = rs["record_id"] if rs else 1

print(f"=== ЧТЕНИЯ (салон 1; client_id={cid}, record_id={rec_id}) ===")
check("is_admin(948205934)", lambda: db.is_admin(948205934))
check("list_admins()", lambda: db.list_admins())
check("get_setting('__none')", lambda: db.get_setting("__none"))
check("get_salon_expenses(today)", lambda: db.get_salon_expenses("2026-06-13"))
check("get_record_state(rec_id)", lambda: db.get_record_state(rec_id))
check("client_has_loyalty_backfill(cid)", lambda: db.client_has_loyalty_backfill(cid))
check("get_client_by_id(cid)", lambda: (db.get_client_by_id(cid) or {}).get("tenant_id"))
check("loyalty_balance(cid)", lambda: db.loyalty_balance(cid))
check("get_active_subscription_for_client(cid)", lambda: db.get_active_subscription_for_client(cid))
check("get_master_by_chat_id(0)", lambda: db.get_master_by_chat_id(0))
check("cutmatch_count_today(0)", lambda: db.cutmatch_count_today(0))
check("get_web_session('nope')", lambda: db.get_web_session("nope"))

print("=== ЗАПИСИ (упор на 5 правок + OR IGNORE + lastrowid) ===")
check("set_setting (ON CONFLICT tenant_id,key)", lambda: db.set_setting("__saas_test", "hi"))
check("  → get_setting обратно == 'hi'", lambda: db.get_setting("__saas_test"))
check("add_admin OR IGNORE (1й раз True)", lambda: db.add_admin(7000000001, added_by=1))
check("add_admin OR IGNORE (2й раз False)", lambda: db.add_admin(7000000001, added_by=1))
check("upsert_record_state (ON CONFLICT t,rec) #1", lambda: db.upsert_record_state(8888888801, 1, "2026-06-13T10:00", "sig", 1))
check("upsert_record_state #2 (обновление)", lambda: db.upsert_record_state(8888888801, 2, "2026-06-13T11:00", "sig2", 1))
check("set_client_history_cache (OR REPLACE)", lambda: db.set_client_history_cache(cid, [{"t": 1}]))
check("cutmatch_incr_today #1 (ON CONFLICT t,u,day)", lambda: db.cutmatch_incr_today(7000000002))
check("cutmatch_incr_today #2 → 2", lambda: db.cutmatch_incr_today(7000000002))
check("add_salon_expense → id (lastrowid)", lambda: db.add_salon_expense("2099-01-01", "__test", 1))
check("log_ai_advice → id (lastrowid)", lambda: db.log_ai_advice(8888888801, cid, 1, "claude", "совет"))
check("create_web_session (OR REPLACE token)", lambda: db.create_web_session("__saas_tok", chat_id=123))
check("  → get_web_session вернул строку", lambda: (db.get_web_session("__saas_tok") or {}).get("chat_id"))
check("create_web_session повторно (conflict)", lambda: db.create_web_session("__saas_tok", chat_id=456))
check("upsert_client_chat_state", lambda: db.upsert_client_chat_state(cid, client_message="привет"))

print("=== ИЗОЛЯЦИЯ: салон 2 не видит данные салона 1 ===")
db.set_tenant(2)
check("get_client_by_id(cid) для салона 2 == None", lambda: db.get_client_by_id(cid))
check("cutmatch_count_today(7000000002) салон2 == 0", lambda: db.cutmatch_count_today(7000000002))

print("=== очистка тестовых записей ===")
db.set_tenant(1)
with db._db() as c:
    c.execute("DELETE FROM settings WHERE key = ?", ("__saas_test",))
    c.execute("DELETE FROM admins WHERE telegram_user_id = ?", (7000000001,))
    c.execute("DELETE FROM record_state WHERE record_id = ?", (8888888801,))
    c.execute("DELETE FROM cutmatch_usage WHERE user_id = ?", (7000000002,))
    c.execute("DELETE FROM salon_expenses WHERE date = ?", ("2099-01-01",))
    c.execute("DELETE FROM ai_advice_log WHERE record_id = ?", (8888888801,))
    c.execute("DELETE FROM web_sessions WHERE token = ?", ("__saas_tok",))
print("очищено.")
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
