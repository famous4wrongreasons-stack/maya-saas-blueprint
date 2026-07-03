"""Чистые тесты каталога тарифов-конструктора (без БД).
Учитывает DISABLED — временно отключённые фичи/допы (cutmatch, video_analytics)."""
import plan_catalog as pc

PASS = FAIL = 0
def ok(label, cond):
    global PASS, FAIL
    print(f"  {'✓' if cond else '✗'} {label}"); PASS += bool(cond); FAIL += (not cond)

print("=== состав тарифов ===")
ok("start: есть запись и бренд", pc.has_feature("start", [], "booking") and pc.has_feature("start", [], "branding"))
ok("start: НЕТ аналитики/журнала", not pc.has_feature("start", [], "analytics") and not pc.has_feature("start", [], "journal"))
ok("pro: есть кабинет+аналитика+журнал", all(pc.has_feature("pro", [], k) for k in ("staff_cabinet","analytics","journal")))
ok("max: есть приоритетная поддержка", pc.has_feature("max", [], "priority_support"))
ok("ни один тариф БЕЗ допа не даёт ai_chatbot", not any(pc.has_feature(p, [], "ai_chatbot") for p in pc.PLAN_ORDER))

print("=== DISABLED: cutmatch + video_analytics отключены ===")
ok("max БОЛЬШЕ не содержит видеоаналитику (DISABLED)", not pc.has_feature("max", [], "video_analytics"))
ok("cutmatch недоступен даже как доп", not pc.has_feature("pro", ["cutmatch"], "cutmatch"))
ok("video_analytics недоступен даже как доп", not pc.has_feature("pro", ["video_analytics"], "video_analytics"))
ok("DISABLED = {cutmatch, video_analytics}", pc.DISABLED == {"cutmatch", "video_analytics"})

print("=== допы (продаётся только ai_chatbot) ===")
ok("start + ai_chatbot → есть ai_chatbot", pc.has_feature("start", ["ai_chatbot"], "ai_chatbot"))
ok("в публичном каталоге ровно 1 доп (ai_chatbot)",
   [a["key"] for a in pc.public_catalog()["addons"]] == ["ai_chatbot"])
ok("в каталоге НЕТ cutmatch/video_analytics среди допов",
   not any(a["key"] in pc.DISABLED for a in pc.public_catalog()["addons"]))
ok("ни один тариф в каталоге не перечисляет отключённые фичи",
   not any(set(p["features"]) & pc.DISABLED for p in pc.public_catalog()["plans"]))

print("=== 2 тарифа: «Сеть»(max) свёрнута в «Салон»(pro) и скрыта из продажи ===")
ok("HIDDEN_PLANS = {max}", pc.HIDDEN_PLANS == {"max"})
ok("PUBLIC_PLAN_ORDER = [start, pro]", pc.PUBLIC_PLAN_ORDER == ["start", "pro"])
ok("max всё ещё в PLANS (не удалён — нужен для БД/биллинга)", "max" in pc.PLANS)
ok("pro теперь с приоритетной поддержкой (вобрал max)", pc.has_feature("pro", [], "priority_support"))
ok("pro без лимита мастеров", pc.PLANS["pro"]["max_masters"] is None)
ok("start: до 5 мастеров", pc.PLANS["start"]["max_masters"] == 5)

print("=== цены (2 тарифа: Запись 990 / Салон 2490) ===")
ok("start = 990", pc.monthly_price("start", []) == 990)
ok("pro = 2490", pc.monthly_price("pro", []) == 2490)
ok("max (скрыт) = 8990 — цена в коде прежняя", pc.monthly_price("max", []) == 8990)
ok("pro + ai_chatbot = 4480", pc.monthly_price("pro", ["ai_chatbot"]) == 4480)
ok("pro + cutmatch = 2490 (доп отключён, не тарифицируется)", pc.monthly_price("pro", ["cutmatch"]) == 2490)
ok("pro + video_analytics = 2490 (доп отключён)", pc.monthly_price("pro", ["video_analytics"]) == 2490)
ok("pro + ai_chatbot + (cutmatch,video отключены) = 4480",
   pc.monthly_price("pro", ["ai_chatbot","cutmatch","video_analytics"]) == 4480)

print("=== метрика чат-бота (квота 150 + сверх 15₽, авто-списание) ===")
ok("ai_chatbot — метрик (150 диалогов, сверх 15₽)", pc.metered_allowance("ai_chatbot")["quota"]==150
   and pc.metered_allowance("ai_chatbot")["overage_rub"]==15)
ok("чат-бот billing=auto (авто-списание сверх квоты)", pc.metered_allowance("ai_chatbot").get("billing")=="auto")
ok("чат-бот 60 диалогов в пределах квоты → 1990", pc.metered_revenue("ai_chatbot", 60) == 1990)
ok("чат-бот 250 диалогов → 1990+100×15=3490", pc.metered_revenue("ai_chatbot", 250) == 3490)
ok("в каталоге метрика только у 1 допа (чат-бот)",
   sum(1 for a in pc.public_catalog()["addons"] if a.get("metered")) == 1)

print("=== публичный каталог для фронта ===")
cat = pc.public_catalog()
ok("2 тарифа в каталоге (max скрыт)", len(cat["plans"]) == 2)
ok("в каталоге ключи только start+pro", [p["key"] for p in cat["plans"]] == ["start", "pro"])
ok("1 доп в каталоге", len(cat["addons"]) == 1)
ok("у каждой фичи тарифа есть человекочитаемое имя",
   all(f in cat["features"] for p in cat["plans"] for f in p["features"]))
ok("отключённых фич нет в словаре features каталога",
   not (set(cat["features"]) & pc.DISABLED))

print(f"\nИТОГО: {PASS} ok, {FAIL} fail")
import sys; sys.exit(1 if FAIL else 0)
