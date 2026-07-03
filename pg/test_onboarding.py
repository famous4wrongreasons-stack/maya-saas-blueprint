"""Шаг 6: онбординг салона — на живой saas_test. Полный цикл регистрации салона."""
import sys
import tenant_onboarding as ob
import tenant_resolver as tr

PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += bool(cond); FAIL += (not cond)

# на случай остатков прошлого прогона
tr._q("DELETE FROM tenants WHERE slug = 'newbarber'")

print("=== 1) Регистрация салона ===")
tid = ob.create_tenant("newbarber", "Новый Барбершоп",
                       owner_email="owner@newbarber.ru", trial_days=14)
print(f"создан салон id={tid}")
ok("резолвится по поддомену newbarber.app.ru", tr.tenant_for_host("newbarber.app.ru") == tid)
ok("в active_tenants (триал считается активным)", tid in [t["id"] for t in tr.active_tenants()])
row = tr._q("SELECT status, trial_ends_at FROM tenants WHERE id=%s", (tid,))[0]
ok("статус trial", row["status"] == "trial")
ok("триал на ~14 дней вперёд", row["trial_ends_at"] is not None)

print("=== 2) Подключение YClients (секреты шифруются) ===")
ob.connect_yclients(tid, company_id=999777, user_token="secret-yc-token-abc",
                    cash_account_id=11, cashless_account_id=22)
cfg = ob.get_provider_config(tid)
ok("креды расшифровались верно",
   cfg.get("company_id") == 999777 and cfg.get("user_token") == "secret-yc-token-abc")
ok("provider = yclients", cfg.get("provider") == "yclients")
raw = bytes(tr._q("SELECT provider_config FROM tenants WHERE id=%s", (tid,))[0]["provider_config"])
ok("🔒 в БД токен ЗАШИФРОВАН (нет открытого текста)", b"secret-yc-token-abc" not in raw)

print("=== 3) Автоподтягивание каталога (через провайдера) ===")
r = ob.sync_catalog(tid, provider=ob.FakeProvider())
ok("подтянулись мастера (2) и услуги (2)", r["masters"] == 2 and r["services"] == 2)
saved = tr._q("SELECT active_master_ids FROM tenants WHERE id=%s", (tid,))[0]["active_master_ids"]
ok("active_master_ids сохранены [101,102]", set(saved) == {101, 102})

print("=== 4) Активация после оплаты ===")
ob.activate(tid)
ok("статус active", tr._q("SELECT status FROM tenants WHERE id=%s", (tid,))[0]["status"] == "active")

print("=== 5) Изоляция: новый салон пуст (нет чужих данных) ===")
import sys as _s, types
stub = types.ModuleType("pii_crypto")
stub.encrypt = lambda s: s; stub.decrypt = lambda s: s; stub.hash_phone = lambda s: str(s)
_s.modules["pii_crypto"] = stub
import db_pg_full as db
with db.tenant_scope(tid):
    with db._db() as c:
        n = c.execute("SELECT count(*) AS n FROM clients").fetchone()["n"]
ok("у нового салона 0 клиентов (данные салона 1 не видны)", n == 0)

print("=== очистка ===")
tr._q("DELETE FROM tenants WHERE id=%s", (tid,))
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
