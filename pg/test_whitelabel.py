"""Шаг 8: white-label — бренд по поддомену + строгая проверка, что секреты НЕ утекают."""
import sys, json
import tenant_config as cfg, tenant_onboarding as ob, tenant_resolver as tr

PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += bool(cond); FAIL += (not cond)

tr._q("DELETE FROM tenants WHERE slug IN ('barbera','barberb')")
a = ob.create_tenant("barbera", "Барбер А")
b = ob.create_tenant("barberb", "Барбер Б")
cfg.set_branding(a, name="Барбер А", logo_url="https://cdn/a.png",
                 accent_color="#c8a96a", city="Ставрополь", tagline="Мужская классика")
cfg.set_branding(b, name="Барбер Б", logo_url="https://cdn/b.png",
                 accent_color="#2e6cf0", city="Москва")
# у салона А подключён YClients с СЕКРЕТНЫМ токеном — он НЕ должен попасть в публичный конфиг
ob.connect_yclients(a, company_id=111, user_token="TOPSECRET-A-TOKEN")

print("=== бренд по поддомену ===")
ca = cfg.public_config("barbera.app.ru")
cb = cfg.public_config("barberb.app.ru:443")
ok("host A → бренд А (имя+цвет+слоган)",
   ca["brand"]["name"] == "Барбер А" and ca["brand"]["accent_color"] == "#c8a96a"
   and ca["brand"]["tagline"] == "Мужская классика")
ok("host B → бренд Б (город Москва)", cb["brand"]["name"] == "Барбер Б" and cb["brand"]["city"] == "Москва")
ok("бренды РАЗНЫЕ (лого не совпадает)", ca["brand"]["logo_url"] != cb["brand"]["logo_url"])
ok("неизвестный поддомен → None", cfg.public_config("nope.app.ru") is None)
ok("www/apex → None", cfg.public_config("www.app.ru") is None)
ok("active-флаг для триала = True", ca["active"] is True)

print("=== 🔒 безопасность: публичный конфиг без секретов ===")
blob_a = json.dumps(ca, ensure_ascii=False)
ok("токен YClients НЕ в конфиге", "TOPSECRET-A-TOKEN" not in blob_a)
ok("нет ключей provider_config/billing/owner",
   all(k not in ca for k in ("provider_config", "billing_method_id", "owner_email"))
   and "provider_config" not in blob_a)
ok("числовой tenant_id наружу не отдаётся", "tenant_id" not in ca)
ok("в конфиге только slug/brand/active",
   set(ca.keys()) == {"slug", "brand", "active"})

print("=== блок отражается во флаге ===")
ob.set_status(a, "suspended")
ok("suspended → active=False (фронт покажет «недоступно»)",
   cfg.public_config("barbera.app.ru")["active"] is False)
ok("но бренд всё ещё отдаётся (для страницы оплаты)",
   cfg.public_config("barbera.app.ru")["brand"]["name"] == "Барбер А")

print("=== очистка ===")
tr._q("DELETE FROM tenants WHERE id IN (%s,%s)", (a, b))
print(f"\nИТОГ: прошло {PASS}, упало {FAIL}")
sys.exit(1 if FAIL else 0)
