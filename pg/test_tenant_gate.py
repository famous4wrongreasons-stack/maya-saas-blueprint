# -*- coding: utf-8 -*-
"""Тесты tenant_gate: паритет МЭ (ни один живой роут не блокируется),
гейтинг по тарифам, payload для фронта. PG не нужен — tr._q фейковый."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tenant_resolver as tr  # noqa: E402

# ── фейк БД: план и допы тенантов ──
_PLANS = {1: "max", 2: "start", 3: "pro"}
_ADDONS = {1: ["ai_chatbot"], 2: [], 3: []}


def _fake_q(sql, params=()):
    if sql.startswith("SELECT plan"):
        tid = params[0]
        return [{"plan": _PLANS[tid]}] if tid in _PLANS else []
    if sql.startswith("SELECT addon_key"):
        return [{"addon_key": a} for a in _ADDONS.get(params[0], [])]
    if sql.startswith("SELECT count"):
        return []
    raise AssertionError("неожиданный SQL: " + sql)


tr._q = _fake_q

import tenant_gate as tg      # noqa: E402
import plan_catalog as pc     # noqa: E402

# Живая таблица роутов прода (снята с webhook_server.py 2026-07-03).
PROD_ROUTES = """/api/applogin/poll /api/applogin/start /api/auth/phone/start
/api/auth/phone/verify /api/auth/status /api/auth/vk /api/auth/vk-sdk
/api/booking/prefill /api/cabinet/link-phone /api/cabinet/me
/api/cabinet/me-via-login /api/cabinet/me-via-session /api/cabinet/notify-prefs
/api/cert/create /api/chat /api/chat/stream /api/consent/status
/api/consent/submit /api/god/billing /api/god/health /api/god/overview
/api/god/subscribers /api/me/photo /api/nearest-slot /api/panel/broadcast
/api/panel/client_search /api/panel/daily_report /api/panel/dashboard
/api/panel/job/run /api/panel/journal /api/panel/journal_add_service
/api/panel/journal_attendance /api/panel/journal_cancel /api/panel/journal_create
/api/panel/journal_pay /api/panel/journal_record /api/panel/journal_reschedule
/api/panel/journal_set_client_data /api/panel/journal_set_client_name
/api/panel/journal_set_services /api/panel/managers /api/panel/master/day
/api/panel/master/overview /api/panel/masters_stats /api/panel/me
/api/panel/my_earnings /api/panel/redeem /api/panel/report_pdf
/api/panel/reviews /api/panel/salary /api/panel/salon_stats
/api/panel/salon_today /api/panel/team /api/panel/team_chat/delete
/api/panel/team_chat/fetch /api/panel/team_chat/normalize_voice
/api/panel/team_chat/send /api/panel/waitlist /api/promo_gift
/api/push/subscribe /api/realtime /api/set-visit-mood /api/sub/create
/api/tips/sent /yclients-webhook""".split()


def test_parity_me_nothing_blocked():
    """МЭ (max + ai_chatbot): ни один живой прод-роут не блокируется."""
    blocked = [r for r in PROD_ROUTES if tg.check_path(1, r) is not None]
    assert blocked == [], f"у МЭ заблокировано: {blocked}"


def test_start_plan_blocks_pro_features():
    denied = tg.check_path(2, "/api/panel/broadcast")
    assert denied and denied["error"] == "feature_locked"
    assert denied["feature"] == "tg_marketing"
    assert denied["need"]["plan"] == "pro" and denied["need"]["price_rub"] == 2490
    assert "Салон" in denied["message"]
    # журнал и аналитика тоже закрыты на start
    assert tg.check_path(2, "/api/panel/journal_create")["feature"] == "journal"
    assert tg.check_path(2, "/api/panel/salary")["feature"] == "analytics"
    # а ядро — открыто
    for r in ("/api/cabinet/me", "/api/booking/prefill", "/api/auth/status",
              "/api/panel/me", "/api/god/overview"):
        assert tg.check_path(2, r) is None, r


def test_ai_chatbot_is_addon_not_plan():
    denied = tg.check_path(3, "/api/chat/stream")  # pro без допа
    assert denied and denied["feature"] == "ai_chatbot"
    assert denied["need"]["addon"] == "ai_chatbot"
    assert "доп" in denied["message"].lower()
    # подключили доп → открыто (и голос тоже)
    _ADDONS[3] = ["ai_chatbot"]
    try:
        assert tg.check_path(3, "/api/chat/stream") is None
        assert tg.check_path(3, "/api/realtime") is None
    finally:
        _ADDONS[3] = []


def test_pro_plan_opens_salon_features():
    for r in ("/api/panel/broadcast", "/api/panel/journal", "/api/panel/salary",
              "/api/panel/team_chat/send", "/api/cert/create", "/api/panel/redeem"):
        assert tg.check_path(3, r) is None, r


def test_longest_prefix_and_unmapped():
    assert tg.feature_for_path("/api/panel/team_chat/send") == "staff_cabinet"
    assert tg.feature_for_path("/api/panel/me") is None
    assert tg.feature_for_path("/api/unknown") is None
    assert tg.feature_for_path("/api/chat?x=1") == "ai_chatbot"


def test_disabled_addon_marked_coming_soon():
    need = tg._need_for("cutmatch")
    assert need["addon"] == "cutmatch" and need.get("coming_soon") is True
    assert "скоро" in tg.gate(2, "cutmatch")["message"].lower()


def test_features_payload_shape():
    p = tg.features_payload(3)
    assert p["plan"] == "pro" and p["plan_title"] == "Салон"
    assert "journal" in p["features"] and "ai_chatbot" not in p["features"]
    assert p["monthly_price_rub"] == pc.PLANS["pro"]["price_rub"]
    assert set(p["disabled"]) == pc.DISABLED


if __name__ == "__main__":
    for name in sorted(list(globals())):
        if name.startswith("test_"):
            globals()[name]()
            print(f"✓ {name}")
    print("OK")
