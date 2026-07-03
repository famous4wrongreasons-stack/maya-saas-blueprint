"""
SaaS Этап 1, Шаг 7 — биллинг подписки САЛОНА на платформу (B2B).

НЕ путать с таблицей subscriptions (абонементы КЛИЕНТОВ салона). Здесь салон платит
ТЕБЕ. Состояние живёт на tenants (status/plan/trial_ends_at/current_period_end/
billing_method_id) + история в tenant_invoices.

Жизненный цикл статуса:
  trial → (оплата) → active → (период истёк, оплата не прошла) → past_due (грейс)
        → (грейс кончился) → suspended → (оплатил) → active.  cancelled — ушёл сам.
Доступ к сервису есть при trial/active/past_due (грейс), нет при suspended/cancelled.

Платёжный шлюз инъектируется (gateway.charge(amount, method_id)) — в тестах FakeGateway,
в проде YooKassa REST (рекуррент по сохранённому payment_method_id). Функции принимают
`now`, чтобы джобы/тесты могли работать с управляемым временем.
"""
from __future__ import annotations
from datetime import timedelta
import tenant_resolver as tr
from plan_catalog import PLANS, ADDONS  # тарифы-конструктор + допы (единый каталог)

PERIOD_DAYS = 30   # длительность оплаченного периода
GRACE_DAYS = 5     # сколько дней грейса в past_due до suspended

ALLOWED_STATUSES = ("trial", "active", "past_due")


def _tenant(tid):
    r = tr._q("SELECT id,status,plan,trial_ends_at,current_period_end,billing_method_id "
              "FROM tenants WHERE id=%s", (tid,))
    return r[0] if r else None


def choose_plan(tenant_id: int, plan: str) -> None:
    if plan not in PLANS:
        raise ValueError(f"Неизвестный тариф: {plan}")
    tr._q("UPDATE tenants SET plan=%s, updated_at=now() WHERE id=%s", (plan, tenant_id))


def access_allowed(tenant_id: int) -> bool:
    """Пускаем ли салон в сервис прямо сейчас (статус поддерживается джобой enforce_access)."""
    t = _tenant(tenant_id)
    return bool(t) and t["status"] in ALLOWED_STATUSES


def save_payment_method(tenant_id: int, method_id: str) -> None:
    tr._q("UPDATE tenants SET billing_method_id=%s, updated_at=now() WHERE id=%s",
          (method_id, tenant_id))


def create_invoice(tenant_id, plan, period_start, period_end, payment_id=None,
                   status="pending") -> int:
    r = tr._q("INSERT INTO tenant_invoices (tenant_id,period_start,period_end,plan,amount_rub,"
              "status,yukassa_payment_id) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
              (tenant_id, period_start, period_end, plan, PLANS[plan]["price_rub"],
               status, payment_id))
    return r[0]["id"]


def mark_invoice_paid(tenant_id, invoice_id, payment_id, paid_at) -> None:
    """Счёт оплачен → продлеваем подписку до конца его периода, статус active."""
    inv = tr._q("UPDATE tenant_invoices SET status='paid', paid_at=%s, yukassa_payment_id=%s "
                "WHERE id=%s AND tenant_id=%s RETURNING period_end",
                (paid_at, payment_id, invoice_id, tenant_id))
    if not inv:
        return
    tr._q("UPDATE tenants SET status='active', current_period_end=%s, updated_at=now() WHERE id=%s",
          (inv[0]["period_end"], tenant_id))


def activate_paid(tenant_id, payment_id, method_id, now) -> None:
    """ПЕРВАЯ успешная оплата (из YooKassa-вебхука): сохраняем способ оплаты,
    создаём оплаченный счёт на период, активируем."""
    t = _tenant(tenant_id)
    plan = t["plan"]
    start, end = now, now + timedelta(days=PERIOD_DAYS)
    inv = create_invoice(tenant_id, plan, start, end, payment_id, status="pending")
    save_payment_method(tenant_id, method_id)
    mark_invoice_paid(tenant_id, inv, payment_id, now)


def charge_due(gateway, now) -> list[dict]:
    """Рекуррент-джоба: салонам, у кого срок подошёл и есть сохранённый способ оплаты,
    автосписываем через gateway. Удача → период продлён; неудача → счёт failed
    (статус подвинет enforce_access). gateway.charge(amount, method_id, description)
    → {'ok':bool, 'payment_id':str}."""
    rows = tr._q("SELECT id,plan,billing_method_id,current_period_end,trial_ends_at "
                 "FROM tenants WHERE status IN ('active','trial','past_due') "
                 "AND billing_method_id IS NOT NULL")
    out = []
    for t in rows:
        due = t["current_period_end"] or t["trial_ends_at"]
        if due is None or due > now:
            continue
        plan = t["plan"]
        res = gateway.charge(PLANS[plan]["price_rub"], t["billing_method_id"],
                             description=f"Подписка «{PLANS[plan]['title']}»")
        start, end = due, due + timedelta(days=PERIOD_DAYS)
        inv = create_invoice(t["id"], plan, start, end, res.get("payment_id"), status="pending")
        if res.get("ok"):
            mark_invoice_paid(t["id"], inv, res.get("payment_id"), now)
            out.append({"tid": t["id"], "ok": True})
        else:
            tr._q("UPDATE tenant_invoices SET status='failed' WHERE id=%s", (inv,))
            out.append({"tid": t["id"], "ok": False})
    return out


def enforce_access(now) -> None:
    """Джоба статусов: истёк оплаченный период/триал → past_due; грейс кончился → suspended."""
    tr._q("UPDATE tenants SET status='past_due', updated_at=now() "
          "WHERE status IN ('trial','active') "
          "AND COALESCE(current_period_end, trial_ends_at) < %s", (now,))
    tr._q("UPDATE tenants SET status='suspended', updated_at=now() "
          "WHERE status='past_due' "
          "AND COALESCE(current_period_end, trial_ends_at) < %s",
          (now - timedelta(days=GRACE_DAYS),))


# ── Платёжные шлюзы ────────────────────────────────────────────────────────
class FakeGateway:
    """Для тестов: без сети, управляемый успех/неудача."""
    def __init__(self, succeed=True):
        self.succeed = succeed
        self.calls = []
    def charge(self, amount, method_id, description=""):
        self.calls.append((amount, method_id))
        return {"ok": self.succeed, "payment_id": f"pay_fake_{len(self.calls)}"}

# Боевой адаптер (каркас): YooKassa REST, автоплатёж по сохранённому payment_method_id.
# В проде у тебя уже есть YooKassa REST для сертификатов — переиспользовать креды/код.
class YooKassaGateway:  # pragma: no cover (сеть; не вызывается в тестах)
    def __init__(self, shop_id, secret_key):
        self.shop_id, self.secret_key = shop_id, secret_key
    def charge(self, amount, method_id, description=""):
        # POST https://api.yookassa.ru/v3/payments  {amount, payment_method_id,
        #   capture:true, description}  с Idempotence-Key; вернуть {'ok':status=='succeeded',
        #   'payment_id':id}. Реализуется на катовере.
        raise NotImplementedError("YooKassaGateway.charge — реализовать на боевом подключении")
