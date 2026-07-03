"""Шаг 7: биллинг — полный жизненный цикл подписки на живой saas_test, с
управляемым временем (триал → оплата → рекуррент → неоплата → грейс → блок → реактивация)."""
import sys
from datetime import datetime, timezone, timedelta
import tenant_onboarding as ob, tenant_billing as bil, tenant_resolver as tr

PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += bool(cond); FAIL += (not cond)

tr._q("DELETE FROM tenant_invoices WHERE tenant_id IN (SELECT id FROM tenants WHERE slug='billtest')")
tr._q("DELETE FROM tenants WHERE slug='billtest'")
tid = ob.create_tenant("billtest", "Billing Test", trial_days=14)

def st():
    return tr._q("SELECT status,current_period_end,billing_method_id FROM tenants WHERE id=%s", (tid,))[0]
def invs():
    return [r["status"] for r in tr._q("SELECT status FROM tenant_invoices WHERE tenant_id=%s ORDER BY id", (tid,))]

print("=== триал ===")
ok("новый салон в trial, доступ есть", st()["status"] == "trial" and bil.access_allowed(tid))
bil.choose_plan(tid, "pro")
ok("тариф pro выбран", tr._q("SELECT plan FROM tenants WHERE id=%s", (tid,))[0]["plan"] == "pro")

print("=== первая оплата ===")
T0 = datetime.now(timezone.utc).replace(microsecond=0)
bil.activate_paid(tid, payment_id="pay_first", method_id="pm_card_123", now=T0)
ok("после оплаты статус active", st()["status"] == "active")
ok("сохранён способ оплаты", st()["billing_method_id"] == "pm_card_123")
ok("один оплаченный счёт", invs() == ["paid"])

print("=== рекуррент ===")
gw = bil.FakeGateway(succeed=True)
bil.charge_due(gw, now=T0 + timedelta(days=10))
ok("до срока автосписания нет", len(gw.calls) == 0)
r = bil.charge_due(gw, now=T0 + timedelta(days=31))
ok("в срок автосписание прошло", len(gw.calls) == 1 and r and r[0]["ok"])
ok("два оплаченных счёта, период продлён", invs() == ["paid", "paid"] and st()["status"] == "active")

print("=== неоплата → грейс → блок ===")
due2 = st()["current_period_end"]
bil.charge_due(bil.FakeGateway(succeed=False), now=due2 + timedelta(seconds=1))
ok("неудачное списание → счёт failed", invs()[-1] == "failed")
ok("период НЕ продлён (до enforce ещё active)", st()["status"] == "active")
bil.enforce_access(now=due2 + timedelta(days=1))
ok("период истёк → past_due", st()["status"] == "past_due")
ok("в грейсе доступ ещё есть", bil.access_allowed(tid))
bil.enforce_access(now=due2 + timedelta(days=bil.GRACE_DAYS + 1))
ok("грейс кончился → suspended", st()["status"] == "suspended")
ok("suspended — доступа НЕТ", not bil.access_allowed(tid))

print("=== реактивация ===")
bil.activate_paid(tid, "pay_again", "pm_card_123", now=due2 + timedelta(days=bil.GRACE_DAYS + 2))
ok("оплатил снова → active + доступ", st()["status"] == "active" and bil.access_allowed(tid))

print("=== очистка ===")
tr._q("DELETE FROM tenant_invoices WHERE tenant_id=%s", (tid,))
tr._q("DELETE FROM tenants WHERE id=%s", (tid,))
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
