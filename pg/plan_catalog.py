"""
SaaS: каталог тарифов-«конструктора».

Модель: салон выбирает ОДИН базовый тариф (start/pro/max — задаёт ядро функций) и
по желанию подключает ДОПЫ-модули (addons) к любому тарифу за отдельную плату.
`has_feature(plan, addons, key)` — единая точка гейтинга фич в приложении/бэке.

🔴 Цены (price_rub) — РЕДАКТИРУЕМЫЕ заглушки. Поставь реальные перед запуском.
Ключи тарифов start/pro/max НЕ переименовывать — на них завязан биллинг и БД
(tenants.plan, tenant_invoices.plan, тесты).
"""
from __future__ import annotations

# ── что вообще умеет продукт (ключ → человекочитаемое название) ───────────────
FEATURES = {
    "booking":          "Онлайн-запись (YClients)",
    "branding":         "Брендированный интерфейс: лого, цвета, тексты",
    "client_app":       "Клиентское приложение: услуги, команда, инфо о салоне",
    "loyalty":          "Баллы и лояльность",
    "shop":             "Магазин: сертификаты и абонементы",
    "tg_basic":         "Telegram-уведомления о записи",
    "tg_marketing":     "Telegram-маркетинг: напоминания, реактивация, ДР, отзывы, рассылки",
    "journal":          "Журнал записи: расписание дня, перенос, статусы",
    "staff_cabinet":    "Кабинет сотрудника: режим мастера, история визитов, чаевые",
    "analytics":        "Аналитика: дневной отчёт, выручка, зарплаты",
    "video_analytics":  "Видеоаналитика: камеры салона",
    "cutmatch":         "CutMatch — ИИ-подбор причёски по фото",
    "ai_chatbot":       "Умный чат-бот: AI-администратор отвечает и записывает клиентов",
    "priority_support": "Приоритетная поддержка",
}

# ── базовые тарифы ───────────────────────────────────────────────────────────
PLANS = {
    "start": {
        "title": "Запись",
        "tagline": "Старт для небольшого салона",
        "price_rub": 990,
        "max_masters": 5,
        "features": [
            "booking", "branding", "client_app", "loyalty", "tg_basic",
        ],
    },
    "pro": {
        "title": "Салон",
        "tagline": "Всё для работы салона",
        "price_rub": 2490,
        "max_masters": None,  # без лимита мастеров (вобрал в себя «Сеть»)
        "features": [
            "booking", "branding", "client_app", "loyalty", "shop", "tg_basic",
            "tg_marketing", "journal", "staff_cabinet", "analytics", "priority_support",
        ],
    },
    # «Сеть» (max) — СВЁРНУТ в «Салон» и СКРЫТ из продажи (см. HIDDEN_PLANS ниже).
    # Ключ не удаляем: на него завязаны БД (tenants.plan), биллинг и тесты. Его
    # не-отключённые фичи (приоритетная поддержка, без лимита) уже перенесены в pro.
    "max": {
        "title": "Сеть",
        "tagline": "Для сети и продвинутых",
        "price_rub": 8990,
        "max_masters": None,  # без лимита мастеров
        "features": [
            "booking", "branding", "client_app", "loyalty", "shop", "tg_basic",
            "tg_marketing", "journal", "staff_cabinet", "analytics",
            "video_analytics", "priority_support",
        ],
    },
}

# ── допы-модули (подключаются к любому базовому тарифу) ───────────────────────
ADDONS = {
    "ai_chatbot": {
        "title": "Умный чат-бот",
        "tagline": "AI-администратор: отвечает клиентам, записывает, делает апсейл",
        "price_rub": 1990,
        "features": ["ai_chatbot"],
        # Модель Sonnet (Haiku туповат для чата). 1 ДИАЛОГ = вся переписка с одним
        # клиентом за сутки (не реплика). В цену допа включено `quota` диалогов/мес;
        # сверх — АВТО-списание `overage_rub` за диалог (бот не встаёт), с потолком и
        # алертами на 120/150 (политика — на стороне биллинга). Себестоимость диалога
        # ~10 ₽ (Sonnet), 15 ₽ держит ~30-50% маржи на сверх-лимите.
        "metered": {"unit": "dialog", "quota": 150, "overage_rub": 15,
                     "model": "sonnet", "billing": "auto"},
    },
    "cutmatch": {
        "title": "CutMatch",
        "tagline": "ИИ-подбор причёски по фото клиента",
        "price_rub": 1990,
        "features": ["cutmatch"],
        # Метрика: в цену включено `quota` консультаций/мес, сверх — `overage_rub` за шт.
        # Себестоимость считаем по `quality` HD-кадра (medium ≈ $0.063), чтобы держать маржу.
        "metered": {"unit": "consultation", "quota": 40, "overage_rub": 50, "quality": "medium"},
    },
    "video_analytics": {
        "title": "Видеоаналитика",
        "tagline": "Камеры салона (в тариф «Сеть» уже входит)",
        "price_rub": 2500,
        "features": ["video_analytics"],
    },
}

PLAN_ORDER = ["start", "pro", "max"]
ADDON_ORDER = ["ai_chatbot", "cutmatch", "video_analytics"]

# 🔴 СКРЫТЫЕ ТАРИФЫ: не продаём и не показываем на витринах (сейчас 2 тарифа —
# «Запись» + «Салон»). Ключ остаётся в PLANS (биллинг/БД/тесты), но из публичного
# каталога исключён. Вернуть «Сеть» в продажу = убрать "max" отсюда.
HIDDEN_PLANS = {"max"}
PUBLIC_PLAN_ORDER = [p for p in PLAN_ORDER if p not in HIDDEN_PLANS]

# 🔴 ВРЕМЕННО ОТКЛЮЧЕНО (не продаём и не показываем на витринах, пока Стас не даст
# команду включить). Включить обратно = убрать ключ отсюда (и фронты, и гейтинг
# тянут состав через public_catalog / features_for, поэтому одного места достаточно
# для бэкенда; в demo-HTML встроены копии каталога — их перегенерить из public_catalog).
DISABLED = {"cutmatch", "video_analytics"}


def _norm_addons(addons) -> list[str]:
    """Только известные ВКЛЮЧЁННЫЕ допы, без дублей, в каноничном порядке."""
    s = set(a for a in (addons or ()) if a in ADDONS and a not in DISABLED)
    return [a for a in ADDON_ORDER if a in s]


def plan_features(plan: str) -> set[str]:
    """Фичи тарифа без временно отключённых (DISABLED)."""
    return set(f for f in PLANS.get(plan, {}).get("features", []) if f not in DISABLED)


def features_for(plan: str, addons=()) -> set[str]:
    """Итоговый набор фич = фичи тарифа ∪ фичи подключённых допов."""
    feats = plan_features(plan)
    for a in _norm_addons(addons):
        feats.update(ADDONS[a]["features"])
    return feats


def has_feature(plan: str, addons, key: str) -> bool:
    return key in features_for(plan, addons)


def addon_redundant(plan: str, addon_key: str) -> bool:
    """True, если доп не имеет смысла — все его фичи уже есть в тарифе."""
    if addon_key not in ADDONS:
        return True
    return set(ADDONS[addon_key]["features"]).issubset(plan_features(plan))


def monthly_price(plan: str, addons=()) -> int:
    """Цена в месяц = тариф + допы (базовая цена). Доп, чьи фичи уже в тарифе, не тарифицируется.
    Сверх-квота метрик-допов (CutMatch) считается отдельно через metered_revenue()."""
    total = PLANS.get(plan, {}).get("price_rub", 0)
    for a in _norm_addons(addons):
        if not addon_redundant(plan, a):
            total += ADDONS[a]["price_rub"]
    return total


def addon_meta(addon_key: str):
    return ADDONS.get(addon_key)


def metered_allowance(addon_key: str):
    """Параметры метрики допа: {unit, quota, overage_rub, quality} или None для безлимитных."""
    return (ADDONS.get(addon_key) or {}).get("metered")


def metered_revenue(addon_key: str, used_units: int) -> int:
    """Выручка метрик-допа за месяц = базовая цена + (использовано − квота) × overage.
    Для безлимитных допов и в пределах квоты возвращает базовую цену."""
    a = ADDONS.get(addon_key)
    if not a:
        return 0
    rev = a["price_rub"]
    m = a.get("metered")
    if m and used_units > m["quota"]:
        rev += (used_units - m["quota"]) * m["overage_rub"]
    return rev


def public_catalog() -> dict:
    """Сериализуемый каталог для фронта (страница тарифов / онбординг).
    Временно отключённые фичи/допы (DISABLED) и скрытые тарифы (HIDDEN_PLANS) НЕ
    попадают в выдачу."""
    return {
        "features": {k: v for k, v in FEATURES.items() if k not in DISABLED},
        "plans": [
            {"key": k, "title": PLANS[k]["title"], "tagline": PLANS[k]["tagline"],
             "price_rub": PLANS[k]["price_rub"], "max_masters": PLANS[k]["max_masters"],
             "features": [f for f in PLANS[k]["features"] if f not in DISABLED]}
            for k in PUBLIC_PLAN_ORDER
        ],
        "addons": [
            {"key": k, "metered": ADDONS[k].get("metered"),
             **{f: ADDONS[k][f] for f in ("title", "tagline", "price_rub", "features")}}
            for k in ADDON_ORDER if k not in DISABLED
        ],
    }
