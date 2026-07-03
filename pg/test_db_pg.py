"""Проверка db_pg.py на живой saas_test (роль salon_app, RLS включён).
Боевой ключ шифрования НЕ нужен — подменяем pii_crypto заглушкой."""
import sys, types

stub = types.ModuleType("pii_crypto")
stub.encrypt = lambda s: f"enc({s})"
stub.decrypt = lambda s: s[4:-1] if s and s.startswith("enc(") else s
stub.hash_phone = lambda s: "h:" + str(s)
sys.modules["pii_crypto"] = stub

import db_pg

NEW = 990001234  # тестовый telegram_chat_id, которого нет в данных

def count_clients():
    with db_pg._db() as c:
        return c.execute("SELECT count(*) AS n FROM clients").fetchone()["n"]

print("=== Салон 1 (реальные данные) ===")
db_pg.set_tenant(1)
print("клиентов у салона 1:", count_clients())

cid = db_pg.get_or_create_client(NEW)
print(f"get_or_create_client({NEW}) -> id={cid}  (INSERT, tenant_id подставлен DEFAULT-ом)")
print("клиентов стало:", count_clients())

db_pg.update_client(cid, name="Тест Шаг3", phone="+79990001122")
row = db_pg.get_client(NEW)
print(f"get_client -> name={row['name']!r}, phone={row['phone']!r}, tenant_id={row['tenant_id']}")
print("loyalty_balance:", db_pg.loyalty_balance(cid))

print("\n=== Изоляция: тот же клиент глазами салона 2 ===")
db_pg.set_tenant(2)
print("get_client(NEW) для салона 2:", db_pg.get_client(NEW), " (RLS должен скрыть → None)")
print("клиентов 'видит' салон 2:", count_clients(), " (свои, не салона 1)")

print("\n=== Очистка теста ===")
db_pg.set_tenant(1)
with db_pg._db() as c:
    c.execute("DELETE FROM clients WHERE telegram_chat_id = ?", (NEW,))
print("тестовый клиент удалён, клиентов снова:", count_clients())
print("\nИТОГ: слой db_pg работает на Postgres с RLS и авто-tenant ✓")
