"""
SaaS Шаг 4 — резолвинг салона (tenant) по входящему запросу.

Определяет, какому салону принадлежит обращение, ДО работы с данными:
  • Telegram: выделенный бот (токен→салон) ИЛИ общий бот (ссылка ?start=salon_<id>
    запоминает chat→салон; дальше — по сохранённой привязке);
  • Сайт/PWA: по поддомену (Host → tenants.slug);
  • Веб-сессия: tenant_id лежит прямо в строке web_sessions (резолвить не нужно).

Эти запросы идут по ГЛОБАЛЬНЫМ таблицам (tenants/tg_chat_binding/bot_registry),
которые НЕ под RLS, поэтому соединение здесь БЕЗ app.tenant_id.
Вернув tenant_id, дальше код вызывает db.set_tenant(tid) и работает как обычно.
"""
from __future__ import annotations
import hashlib
import psycopg2
import psycopg2.extras

PG = dict(host="/Users/stanislavmosin/saas_pg_rehearsal/pgdata", port=54329,
          user="salon_app", dbname="saas_test")

_ACTIVE = ("trial", "active", "past_due")  # past_due ещё пускаем (грейс), suspended/cancelled — нет


def _q(sql, params=()):
    conn = psycopg2.connect(**PG)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


# ── Отдельные резолверы ────────────────────────────────────────────────────
def tenant_for_host(host: str) -> int | None:
    """'malesthetic.app.ru:443' → салон по slug 'malesthetic'. Апекс/www → None."""
    if not host:
        return None
    label = host.split(":")[0].strip().lower().split(".")[0]
    if not label or label in ("www", "app", "localhost"):
        return None
    rows = _q("SELECT id FROM tenants WHERE slug = %s AND status <> 'cancelled'", (label,))
    return rows[0]["id"] if rows else None


def tenant_for_bot_token(token: str) -> int | None:
    """Выделенный бот: токен → салон (по хэшу токена)."""
    if not token:
        return None
    rows = _q("SELECT tenant_id FROM bot_registry WHERE token_hash = %s", (_token_hash(token),))
    return rows[0]["tenant_id"] if rows else None


def tenant_for_chat(chat_id: int) -> int | None:
    """Общий бот: сохранённая привязка chat → салон."""
    rows = _q("SELECT tenant_id FROM tg_chat_binding WHERE chat_id = %s", (chat_id,))
    return rows[0]["tenant_id"] if rows else None


def bind_chat(chat_id: int, tenant_id: int) -> None:
    """Запомнить, что этот чат относится к салону (перезаписывает прежнюю привязку)."""
    _q("INSERT INTO tg_chat_binding (chat_id, tenant_id) VALUES (%s, %s) "
       "ON CONFLICT (chat_id) DO UPDATE SET tenant_id = excluded.tenant_id, bound_at = now()",
       (chat_id, tenant_id))


def _tenant_from_payload(payload: str) -> int | None:
    """🔴 АУДИТ-ФИКС: deeplink-пейлоад трактуем ТОЛЬКО как slug салона (его публичный
    «логин»), а НЕ как числовой id. Последовательные id 1..N угадываемы и давали
    захват чужого салона перебором. Боевой апгрейд — подписанный invite-токен
    (HMAC+TTL), который нельзя подделать для произвольного салона."""
    p = (payload or "").strip().lower()
    if not p:
        return None
    rows = _q("SELECT id FROM tenants WHERE slug = %s AND status <> 'cancelled'", (p,))
    return rows[0]["id"] if rows else None


# ── Главный резолвер для Telegram ──────────────────────────────────────────
def resolve_telegram(chat_id: int, start_payload: str = None,
                     bot_token: str = None) -> int | None:
    """Определить салон входящего телеграм-сообщения. Приоритет:
      1) выделенный бот (токен) — однозначно;
      2) deeplink ?start=salon_<id>/<slug> — запоминаем привязку и возвращаем;
      3) сохранённая привязка чата (общий бот).
    Возвращает tenant_id или None (не смогли определить — например, человек написал
    общему боту без ссылки и раньше не привязывался)."""
    if bot_token:
        tid = tenant_for_bot_token(bot_token)
        if tid:
            return tid
    existing = tenant_for_chat(chat_id)
    if start_payload:
        tid = _tenant_from_payload(start_payload)
        if tid:
            # 🔴 АУДИТ-ФИКС: привязываем ТОЛЬКО при первом контакте. Если чат уже
            # привязан к другому салону — НЕ перепривязываем молча (это давало
            # захват/угон чата чужой ссылкой). Смена салона — отдельное явное
            # действие пользователя (кнопка «сменить салон»), не deeplink.
            if existing is None:
                bind_chat(chat_id, tid)
                return tid
            return existing
    return existing


def register_bot(token: str, tenant_id: int, username: str = None) -> None:
    """Привязать выделенного бота к салону (white-label)."""
    if not token or not token.strip():
        raise ValueError("register_bot: пустой токен — все «безтокенные» боты схлопнулись бы в один хэш")
    _q("INSERT INTO bot_registry (token_hash, tenant_id, username) VALUES (%s, %s, %s) "
       "ON CONFLICT (token_hash) DO UPDATE SET tenant_id = excluded.tenant_id, "
       "username = excluded.username",
       (_token_hash(token), tenant_id, username))


def active_tenants() -> list[dict]:
    """Салоны, по которым крутим фоновые джобы (напоминания, отчёты и т.п.)."""
    return _q("SELECT id, slug, display_name, status FROM tenants "
              "WHERE status = ANY(%s) ORDER BY id", (list(_ACTIVE),))
