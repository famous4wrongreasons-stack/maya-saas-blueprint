"""АУДИТ-ФИКС: доказываем, что салон НЕ может сослаться (FK) на клиента ЧУЖОГО
салона. До фикса cross-tenant INSERT проходил (FK обходил RLS) — теперь падает."""
import sys, types
stub = types.ModuleType("pii_crypto")
stub.encrypt = lambda s: s; stub.decrypt = lambda s: s; stub.hash_phone = lambda s: str(s)
sys.modules["pii_crypto"] = stub
import db_pg_full as db
from datetime import datetime
now = datetime.now().isoformat(timespec="seconds")
PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += cond; FAIL += (not cond)

# демо-салон №2 + клиент в нём
db.set_tenant(1)
with db._db() as c:
    c.execute("INSERT INTO tenants (slug, display_name, status) VALUES ('demo','Demo','active') "
              "ON CONFLICT (slug) DO UPDATE SET status='active'")
demo = None
with db._db() as c:
    demo = c.execute("SELECT id FROM tenants WHERE slug='demo'").fetchone()["id"]

db.set_tenant(demo)
with db._db() as c:
    foreign_cid = c.execute(
        "INSERT INTO clients (telegram_chat_id, created_at, updated_at) VALUES (?,?,?) RETURNING id",
        (880099, now, now)).fetchone()["id"]
print(f"клиент салона 2: id={foreign_cid}")

print("=== САЛОН 1 пытается сослаться на клиента САЛОНА 2 ===")
blocked = False
try:
    db.set_tenant(1)
    with db._db() as c:
        c.execute("INSERT INTO bookings (client_id, service, master, datetime, created_at) "
                  "VALUES (?,?,?,?,?)", (foreign_cid, "взлом", "x", now, now))
except Exception as e:
    blocked = True
    print(f"   FK заблокировал: {type(e).__name__}: {str(e).splitlines()[0][:80]}")
ok("cross-tenant ссылка на чужого клиента ОТКЛОНЕНА", blocked)

print("=== контроль: ссылка на СВОЕГО клиента работает ===")
worked = False
with db._db() as c:
    own = c.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"]
    cur = c.execute("INSERT INTO bookings (client_id, service, master, datetime, created_at) "
                    "VALUES (?,?,?,?,?) RETURNING id", (own, "стрижка", "x", now, now))
    bid = cur.fetchone()["id"]
    worked = bool(bid)
    c.execute("DELETE FROM bookings WHERE id = ?", (bid,))   # cleanup
ok("ссылка на своего клиента ПРИНЯТА", worked)

print("=== очистка ===")
db.set_tenant(demo)
with db._db() as c:
    c.execute("DELETE FROM clients WHERE id = ?", (foreign_cid,))
db.set_tenant(1)
with db._db() as c:
    c.execute("DELETE FROM tenants WHERE slug = 'demo'")
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
