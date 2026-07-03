"""
SaaS Шаг 5 — обёртки точек входа (тот самый «клей»).

КАЖДЫЙ входящий запрос проходит через одну из этих обёрток: она определяет салон
(резолвер) и входит в db.tenant_scope(tid). Внутри блока весь код (и существующие
db_pg-функции, и yclients через провайдера) работает с данными ровно этого салона.
На выходе из блока контекст гарантированно сбрасывается (tenant_scope) — stale-tenant
не утечёт на следующий запрос.

Боевое применение:
  • Telegram (PTB-handler):
        async def on_message(update, ctx):
            chat = update.effective_user.id
            try:
                with telegram_request(chat, start_payload=_payload(update),
                                      bot_token=ctx.bot.token) as tid:
                    ... вся обработка сообщения ...
            except TenantUnresolved:
                await update.message.reply_text("Откройте бота по ссылке вашего салона 🙂")
  • HTTP (aiohttp-middleware):
        with http_request(host=request.host,
                          session_tenant_id=session.get("tenant_id")) as tid:
            ... отдать данные салона ...
"""
from __future__ import annotations
from contextlib import contextmanager
import tenant_resolver as tr
import tenant_billing as billing
import db_pg_full as db


class TenantUnresolved(Exception):
    """Салон не определён — обработать нечего (бот: попросить ссылку салона; HTTP: 404/лендинг)."""


class SubscriptionInactive(Exception):
    """Салон есть, но подписка suspended/cancelled — фичи закрыты (пустить только на оплату)."""


@contextmanager
def telegram_request(chat_id: int, start_payload: str = None, bot_token: str = None,
                     require_active: bool = False):
    """Резолвит салон входящего телеграм-апдейта и входит в tenant_scope.
    Поднимает TenantUnresolved, если общий бот и салон не определить.
    require_active=True (для фич-роутов) → SubscriptionInactive, если подписка не активна.
    Роуты оплаты/лендинга оставляют require_active=False, чтобы салон мог заплатить."""
    tid = tr.resolve_telegram(chat_id, start_payload=start_payload, bot_token=bot_token)
    if tid is None:
        raise TenantUnresolved(f"telegram chat={chat_id}")
    if require_active and not billing.access_allowed(tid):
        raise SubscriptionInactive(tid)
    with db.tenant_scope(tid):
        yield tid


@contextmanager
def http_request(host: str = None, session_tenant_id: int = None, require_active: bool = False):
    """Резолвит салон HTTP-запроса. 🔴 Приоритет — АВТОРИЗОВАННАЯ сессия
    (web_sessions.tenant_id), а Host — только для неавторизованного лендинга
    (Host подделывается, доверять ему данные нельзя).
    require_active=True (фич-роуты) → SubscriptionInactive при неактивной подписке."""
    tid = session_tenant_id or tr.tenant_for_host(host)
    if tid is None:
        raise TenantUnresolved(f"http host={host}")
    if require_active and not billing.access_allowed(tid):
        raise SubscriptionInactive(tid)
    with db.tenant_scope(tid):
        yield tid
