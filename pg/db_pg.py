"""
SaaS Этап 0 — Шаг 3: слой данных на PostgreSQL с привязкой к салону (tenant).

Будущая замена sqlite-слоя database.py. Главная идея — tenant_id почти НЕВИДИМ
для функций, поэтому перенос ~40 функций почти механический:
  * ЧТЕНИЕ фильтрует RLS по текущему салону (SET app.tenant_id) — WHERE tenant_id
    в запросах НЕ нужен;
  * в INSERT tenant_id подставляется DEFAULT-ом из app.tenant_id — указывать не надо;
  * перенос функции = '?'→'%s' (делает обёртка) + 'lastrowid'→'RETURNING id'.

🔴 Подключаемся НЕ суперпользователем (salon_app), иначе Postgres ИГНОРИРУЕТ RLS.
"""
from __future__ import annotations
import contextvars
from contextlib import contextmanager
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

# Локальная репетиция. На Yandex Managed PG поменять на host/port/пароль из секрета.
PG = dict(host="/Users/stanislavmosin/saas_pg_rehearsal/pgdata", port=54329,
          user="salon_app", dbname="saas_test")

_tenant: contextvars.ContextVar[int | None] = contextvars.ContextVar("tenant_id", default=None)


def set_tenant(tenant_id: int) -> None:
    """Задать текущий салон для последующих обращений к БД (в этом контексте/задаче)."""
    _tenant.set(int(tenant_id))


def current_tenant() -> int | None:
    return _tenant.get()


class _Conn:
    """Тонкая обёртка над psycopg2-соединением: даёт .execute(sql, params) как у
    sqlite — '?'→'%s', экранирует литеральные '%', строки приходят dict-ом (row['col'])."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        # сначала экранируем литеральные % (LIKE '%x%'), потом ? → %s
        sql2 = sql.replace("%", "%%").replace("?", "%s")
        cur = self._raw.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql2, params)
        return cur


@contextmanager
def _db():
    """Соединение под ТЕКУЩИЙ салон: ставит app.tenant_id, commit при успехе,
    rollback при ошибке, всегда закрывает."""
    tid = _tenant.get()
    if tid is None:
        raise RuntimeError("tenant не задан: вызови db_pg.set_tenant(<id>) до работы с БД")
    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as c:
            c.execute("SET app.tenant_id = %s", (str(tid),))
        yield _Conn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════════════
# Порт представительных функций database.py — паттерны переноса.
# Сравни с боевыми: тела почти не изменились, tenant_id нигде не упоминается.
# ══════════════════════════════════════════════════════════════════════════
def get_or_create_client(telegram_chat_id: int) -> int:
    """Внутренний id клиента; создаёт, если нет. (порт database.py:576)"""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if row:
            return row["id"]
        # было: cur.lastrowid → стало: RETURNING id (Postgres-паттерн).
        # tenant_id НЕ указываем — подставится DEFAULT из app.tenant_id.
        row = conn.execute(
            "INSERT INTO clients (telegram_chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?) RETURNING id",
            (telegram_chat_id, _now(), _now()),
        ).fetchone()
        return row["id"]


def update_client(client_id: int, name: str = None, phone: str = None) -> None:
    """Сохранить ПД (шифрованно). (порт database.py:593)"""
    import pii_crypto
    fields, values = [], []
    if name is not None:
        fields.append("name_enc = ?"); values.append(pii_crypto.encrypt(name))
    if phone is not None:
        fields.append("phone_enc = ?"); values.append(pii_crypto.encrypt(phone))
        fields.append("phone_hash = ?"); values.append(pii_crypto.hash_phone(phone))
    if not fields:
        return
    fields.append("updated_at = ?"); values.append(_now()); values.append(client_id)
    with _db() as conn:
        conn.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id = ?", values)


def _client_row_to_dict(row) -> dict:
    import pii_crypto
    d = dict(row)
    d["name"] = pii_crypto.decrypt(d.get("name_enc"))
    d["phone"] = pii_crypto.decrypt(d.get("phone_enc"))
    return d


def get_client(telegram_chat_id: int) -> dict | None:
    """Клиент по Telegram chat_id с расшифрованными ПД. (порт database.py:625)"""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        return _client_row_to_dict(row) if row else None


def loyalty_balance(client_id: int) -> int:
    """Сумма баллов клиента. (порт database.py:2404)"""
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS bal FROM loyalty_transactions "
            "WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return int(row["bal"] or 0)
