# -*- coding: utf-8 -*-
"""
SaaS Шаг 3 — HTTP-слой гейтинга фич по тарифу.

Поверх готовой логики plan_catalog.py + tenant_features.py добавляет то, чего
не хватало для продукта:

  1. ROUTE_FEATURES — карта РЕАЛЬНЫХ роутов боевого webhook_server.py → фича
     (снята с прод-таблицы роутов 2026-07-03, 67 маршрутов).
  2. gate()/check_path() — чистая логика «пустить или 403» с человеческим
     payload'ом: фронт показывает «Доступно на тарифе „Салон"» и апсейл-кнопку.
  3. features_payload() — ответ GET /api/tenant-features: панель прячет
     разделы, которых нет в тарифе (двухуровневая навигация готова к гейтингу).
  4. attach()/gating_middleware() — тонкий aiohttp-адаптер (подключение к
     боевому серверу потом = 2 строки; сейчас НИЧЕГО не подключено).

Плановая модель (см. plan_catalog): start «Запись» → pro «Салон» → скрытый max
«Сеть»; допы ai_chatbot/cutmatch/video_analytics. МЭ при миграции = plan max
+ addon ai_chatbot (тест паритета: ни один живой прод-роут не блокируется).
"""
from __future__ import annotations

import plan_catalog as pc
import tenant_features as tf

# ─────────────────────── карта: роут прода → фича ───────────────────────
# Longest-prefix match. Роуты, которых нет в карте, НЕ гейтятся (core:
# auth, cabinet, consent, push, booking, yclients-webhook, panel/me...).
# god/* — founder-кабинет платформы, тарифом не гейтится by design.
ROUTE_FEATURES: list[tuple[str, str]] = [
    # Telegram-маркетинг (рассылки, отзывы, лист ожидания)
    ("/api/panel/broadcast",      "tg_marketing"),
    ("/api/panel/reviews",        "tg_marketing"),
    ("/api/panel/waitlist",       "tg_marketing"),
    # Журнал записи (11 journal-роутов одним префиксом)
    ("/api/panel/journal",        "journal"),
    # Кабинет сотрудника / команда
    ("/api/panel/master",         "staff_cabinet"),
    ("/api/panel/my_earnings",    "staff_cabinet"),
    ("/api/panel/team",           "staff_cabinet"),   # + team_chat/* тем же префиксом
    ("/api/panel/client_search",  "staff_cabinet"),
    ("/api/panel/managers",       "staff_cabinet"),
    # Аналитика / отчёты / зарплаты
    ("/api/panel/dashboard",      "analytics"),
    ("/api/panel/daily_report",   "analytics"),
    ("/api/panel/masters_stats",  "analytics"),
    ("/api/panel/salary",         "analytics"),
    ("/api/panel/salon_stats",    "analytics"),
    ("/api/panel/salon_today",    "analytics"),
    ("/api/panel/report_pdf",     "analytics"),
    # Магазин (сертификаты, абонементы, гашение)
    ("/api/cert/create",          "shop"),
    ("/api/sub/create",           "shop"),
    ("/api/panel/redeem",         "shop"),
    # AI-администратор (чат + голос) — доп ai_chatbot
    ("/api/chat",                 "ai_chatbot"),
    ("/api/realtime",             "ai_chatbot"),
]


def feature_for_path(path: str) -> str | None:
    """Фича, гейтящая путь (longest-prefix), или None (роут не гейтится)."""
    best = None
    best_len = -1
    p = path.split("?", 1)[0]
    for prefix, feat in ROUTE_FEATURES:
        if p.startswith(prefix) and len(prefix) > best_len:
            best, best_len = feat, len(prefix)
    return best


# ─────────────────────── чистая логика гейта ───────────────────────

def _need_for(feature: str) -> dict:
    """Как салону получить фичу: минимальный публичный тариф или доп."""
    for plan in pc.PUBLIC_PLAN_ORDER:
        if feature in pc.plan_features(plan):
            meta = pc.PLANS[plan]
            return {"plan": plan, "plan_title": meta["title"],
                    "price_rub": meta["price_rub"]}
    for key, addon in pc.ADDONS.items():
        if feature in addon.get("features", ()):
            need = {"addon": key, "addon_title": addon["title"],
                    "price_rub": addon["price_rub"]}
            if key in pc.DISABLED:
                need["coming_soon"] = True
            return need
    return {}


def gate(tenant_id: int, feature: str) -> dict | None:
    """None — пускаем. Иначе — готовое тело 403 для фронта."""
    if tf.has_feature(tenant_id, feature):
        return None
    need = _need_for(feature)
    title = pc.FEATURES.get(feature, feature)
    if need.get("coming_soon"):
        msg = f"Модуль «{title}» скоро появится."
    elif "plan" in need:
        msg = f"«{title}» доступно на тарифе «{need['plan_title']}»."
    elif "addon" in need:
        msg = f"«{title}» подключается допом «{need['addon_title']}»."
    else:
        msg = f"Модуль «{title}» не подключён."
    return {"error": "feature_locked", "feature": feature,
            "feature_title": title, "need": need, "message": msg}


def check_path(tenant_id: int, path: str) -> dict | None:
    """Гейт по пути запроса. None — роут не гейтится или фича доступна."""
    feat = feature_for_path(path)
    if feat is None:
        return None
    return gate(tenant_id, feat)


def features_payload(tenant_id: int) -> dict:
    """Для GET /api/tenant-features: панель прячет недоступные разделы."""
    plan = tf.tenant_plan(tenant_id)
    meta = pc.PLANS.get(plan, {})
    feats = sorted(tf.features_for_tenant(tenant_id))
    return {
        "plan": plan,
        "plan_title": meta.get("title", plan),
        "features": feats,
        "addons": sorted(tf.tenant_addons(tenant_id)),
        "disabled": sorted(pc.DISABLED),
        "monthly_price_rub": tf.monthly_price(tenant_id),
    }


# ─────────────────────── aiohttp-адаптер (подключение потом) ───────────────────────

def attach(app, get_tenant_id) -> None:
    """GET /api/tenant-features. get_tenant_id: async (request) -> int|None."""
    from aiohttp import web

    async def handler(request):
        tid = await get_tenant_id(request)
        if tid is None:
            return web.json_response({"error": "tenant_unresolved"}, status=404)
        return web.json_response(features_payload(tid))

    app.router.add_get("/api/tenant-features", handler)


def gating_middleware(get_tenant_id):
    """aiohttp-middleware: 403 c feature_locked-payload'ом на гейченных роутах.
    Подключение: app.middlewares.append(gating_middleware(resolver))."""
    from aiohttp import web

    @web.middleware
    async def mw(request, handler):
        feat = feature_for_path(request.path)
        if feat is not None:
            tid = await get_tenant_id(request)
            denied = gate(tid, feat) if tid is not None else None
            if denied is not None:
                return web.json_response(denied, status=403)
        return await handler(request)

    return mw
