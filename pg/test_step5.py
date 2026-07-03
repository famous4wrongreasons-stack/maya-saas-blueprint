"""Шаг 5: джобы по всем салонам + обёртки точек входа. На живой saas_test.
Используем ТОЛЬКО tenant_scope (не set_tenant), чтобы вне блоков контекст был пуст."""
import sys, types
stub = types.ModuleType("pii_crypto")
stub.encrypt = lambda s: s; stub.decrypt = lambda s: s; stub.hash_phone = lambda s: str(s)
sys.modules["pii_crypto"] = stub
from datetime import datetime
import db_pg_full as db, tenant_resolver as tr, tenant_jobs as jobs, saas_runtime as rt

now = datetime.now().isoformat(timespec="seconds")
PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += bool(cond); FAIL += (not cond)

# ── setup: салон 2 (active) + 1 клиент + 1 pending-отзыв ──
with db.tenant_scope(1):
    with db._db() as c:
        c.execute("INSERT INTO tenants (slug,display_name,status) VALUES ('demo','Demo','active') "
                  "ON CONFLICT (slug) DO UPDATE SET status='active'")
demo = tr.tenant_for_host("demo.app.ru")
with db.tenant_scope(demo):
    with db._db() as c:
        cid = c.execute("INSERT INTO clients (telegram_chat_id,created_at,updated_at) "
                        "VALUES (?,?,?) RETURNING id", (870001, now, now)).fetchone()["id"]
        c.execute("INSERT INTO review_requests (client_id,record_id,visit_closed_at,send_after,status) "
                  "VALUES (?,?,?,?, 'pending')", (cid, 111, now, now))
print(f"салон 2 (demo) id={demo}, клиент id={cid}\n")

print("=== 1) джоба по ВСЕМ салонам (изоляция данных) ===")
res = jobs.run_for_all_tenants(jobs.tenant_health, label="health")
by = {r["tenant_id"]: r for r in res}
ok("джоба прошла по обоим салонам", set(by) == {1, demo})
ok("салон 1 видит свои 12 клиентов", by[1]["result"]["clients"] == 12)
ok("салон 2 видит 1 клиента (не 12)", by[demo]["result"]["clients"] == 1)
ok("салон 2: 1 pending-отзыв", by[demo]["result"]["pending_reviews"] == 1)
ok("салон 1: 0 pending-отзывов (изоляция)", by[1]["result"]["pending_reviews"] == 0)

print("=== 2) точка входа Telegram ===")
tr.bind_chat(870002, demo)
with rt.telegram_request(870002) as tid:
    ok("telegram_request резолвит → demo", tid == demo)
    with db._db() as c:
        ok("внутри scope видим только клиентов demo (1)",
           c.execute("SELECT count(*) AS n FROM clients").fetchone()["n"] == 1)
def _unresolved(fn):
    try: fn(); return False
    except rt.TenantUnresolved: return True
ok("неизвестный чат → TenantUnresolved", _unresolved(lambda: rt.telegram_request(999999).__enter__()))

print("=== 3) точка входа HTTP ===")
with rt.http_request(host="demo.app.ru:443") as tid:
    ok("http по Host demo → demo", tid == demo)
with rt.http_request(host="malesthetic.app.ru", session_tenant_id=demo) as tid:
    ok("🔒 сессия важнее Host", tid == demo)

print("=== 4) контекст сбрасывается после блока (нет stale-утечки) ===")
leaked = False
try:
    with db._db():
        pass
except RuntimeError:
    leaked = True
ok("вне scope db требует явный салон (RuntimeError)", leaked)

print("=== 5) устойчивость: падение одного салона не валит другие ===")
def flaky(tid):
    if tid == demo:
        raise ValueError("boom")
    return "ok"
b2 = {r["tenant_id"]: r for r in jobs.run_for_all_tenants(flaky)}
ok("салон 1 отработал, несмотря на падение салона 2", b2[1]["ok"] and not b2[demo]["ok"])

print("=== очистка ===")
with db.tenant_scope(demo):
    with db._db() as c:
        c.execute("DELETE FROM review_requests WHERE client_id=?", (cid,))
        c.execute("DELETE FROM clients WHERE id=?", (cid,))
tr._q("DELETE FROM tg_chat_binding WHERE chat_id=870002")
with db.tenant_scope(1):
    with db._db() as c:
        c.execute("DELETE FROM tenants WHERE slug='demo'")
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
