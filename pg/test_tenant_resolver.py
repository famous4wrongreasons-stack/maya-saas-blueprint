"""Проверка tenant_resolver.py на живой saas_test со вторым салоном."""
import sys
import tenant_resolver as tr

PASS = FAIL = 0
def check(label, got, expected):
    global PASS, FAIL
    ok = (got == expected)
    print(f"  {'✓' if ok else '✗'} {label}: got={got!r} expected={expected!r}")
    PASS += (1 if ok else 0); FAIL += (0 if ok else 1)

# демо-салон №2
demo = tr._q("INSERT INTO tenants (slug, display_name, status) VALUES "
             "('demo','Demo Salon','active') ON CONFLICT (slug) DO UPDATE "
             "SET status='active' RETURNING id")[0]["id"]
print(f"демо-салон создан: id={demo}\n")

print("=== по поддомену (Host) ===")
check("malesthetic.app.ru → 1", tr.tenant_for_host("malesthetic.app.ru"), 1)
check("demo.app.ru:443 → demo", tr.tenant_for_host("demo.app.ru:443"), demo)
check("www.app.ru → None", tr.tenant_for_host("www.app.ru"), None)
check("unknown.app.ru → None", tr.tenant_for_host("unknown.app.ru"), None)

print("=== Telegram: deeplink по SLUG + привязка (АУДИТ-ФИКС) ===")
check("resolve(555, start='demo' slug) → demo (первый контакт)",
      tr.resolve_telegram(555, start_payload="demo"), demo)
check("tenant_for_chat(555) запомнился → demo", tr.tenant_for_chat(555), demo)
check("resolve(555) без payload → demo (по привязке)", tr.resolve_telegram(555), demo)
check("🔒 resolve(555, start='malesthetic') НЕ перепривязывает → demo",
      tr.resolve_telegram(555, start_payload="malesthetic"), demo)
check("tenant_for_chat(555) остался demo", tr.tenant_for_chat(555), demo)
check("🔒 числовой salon_<id> больше НЕ угоняет чат → None",
      tr.resolve_telegram(558, start_payload=f"salon_{demo}"), None)
check("свежий чат 556 по slug 'malesthetic' → 1",
      tr.resolve_telegram(556, start_payload="malesthetic"), 1)

print("=== Telegram: выделенный бот (токен) ===")
tr.register_bot("secret-token-xyz", demo, "demo_bot")
check("tenant_for_bot_token → demo", tr.tenant_for_bot_token("secret-token-xyz"), demo)
check("resolve(777, bot_token=...) → demo (токен главнее)",
      tr.resolve_telegram(777, bot_token="secret-token-xyz"), demo)
check("токен НЕ хранится в открытом виде",
      tr._q("SELECT count(*) AS n FROM bot_registry WHERE token_hash = %s",
            ("secret-token-xyz",))[0]["n"], 0)
def _raises(fn):
    try: fn(); return False
    except ValueError: return True
check("🔒 register_bot('') → ValueError (пустой токен)", _raises(lambda: tr.register_bot("", demo)), True)

print("=== негатив и активные салоны ===")
check("resolve(999) неизвестный чат → None", tr.resolve_telegram(999), None)
ids = sorted(t["id"] for t in tr.active_tenants())
check("active_tenants содержит 1 и demo", (1 in ids and demo in ids), True)

print("\n=== очистка ===")
tr._q("DELETE FROM tg_chat_binding WHERE chat_id IN (555,556,558,777)")
tr._q("DELETE FROM bot_registry WHERE tenant_id = %s", (demo,))
tr._q("DELETE FROM tenants WHERE id = %s", (demo,))
print("очищено.")
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
