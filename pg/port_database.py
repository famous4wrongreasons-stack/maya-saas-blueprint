#!/usr/bin/env python3
"""
Портирует боевой database.py (SQLite) → db_pg_full.py (PostgreSQL + tenant).

Стратегия: 155 функций используют общий слой `_db()` и плейсхолдеры '?'. Поэтому
меняем ТОЛЬКО слой подключения + 5 SQL-мест, а остальное работает через обёртку:
  * обёртка _Conn.execute: '?'→'%s', экранирует '%', INSERT OR IGNORE → ON CONFLICT DO NOTHING;
  * обёртка _Cur.lastrowid: через Postgres lastval() — старый cur.lastrowid работает;
  * ЧТЕНИЕ фильтрует RLS, INSERT подставляет tenant_id DEFAULT-ом (см. схему).
Ручных правок 5: 2× INSERT OR REPLACE → ON CONFLICT DO UPDATE, и 3× ON CONFLICT(...)
получают tenant_id (т.к. уникальность стала пер-тенантной).
init_db не трогаем (для PG не нужен — схема в 01_schema_postgres.sql).
"""
import re

SRC = "/Users/stanislavmosin/Desktop/сайт и приложение/ai администратор/database.py"
OUT = __file__.rsplit("/", 1)[0] + "/db_pg_full.py"

NEW_LAYER = '''import re
import psycopg2
import psycopg2.extras
import contextvars'''

OLD_DB = '''@contextmanager
def _db():
    """Соединение с БД: коммитит при успехе, всегда закрывает.

    WAL + busy_timeout: bot.py и webhook_server.py — ДВА процесса на один файл.
    Без WAL (journal_mode=delete) писатель блокирует весь файл, а при busy_timeout=0
    второй процесс сразу падает 'database is locked' и теряет запись (согласие/оплата/
    лояльность). timeout=30 даёт busy-ожидание, WAL снимает конфликт читатель/писатель."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()'''

NEW_DB = '''# ─── Postgres + привязка к салону (SaaS-слой; заменяет sqlite3) ─────────────
PG = dict(host="/Users/stanislavmosin/saas_pg_rehearsal/pgdata", port=54329,
          user="salon_app", dbname="saas_test")

_tenant = contextvars.ContextVar("tenant_id", default=None)


def set_tenant(tenant_id):
    """Текущий салон для последующих обращений к БД (в этом контексте/задаче)."""
    _tenant.set(int(tenant_id))


def current_tenant():
    return _tenant.get()


from contextlib import contextmanager as _contextmanager


@_contextmanager
def tenant_scope(tenant_id):
    """🔴 АУДИТ-ФИКС: ставит салон на время блока и ГАРАНТИРОВАННО сбрасывает —
    чтобы stale-tenant не утёк в следующий запрос на том же потоке/таске.
    Каждая точка входа (telegram-апдейт, HTTP-запрос, джоба) должна оборачивать
    работу в `with tenant_scope(tid):`."""
    token = _tenant.set(int(tenant_id))
    try:
        yield
    finally:
        _tenant.reset(token)


class _Cur:
    """Курсор как у sqlite: fetchone/fetchall/rowcount + lastrowid через lastval()."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        self._cur.execute("SELECT lastval() AS id")
        return self._cur.fetchone()["id"]


class _Conn:
    """conn.execute(sql, params) как у sqlite: '?'→'%s', экранирует литеральные
    '%', INSERT OR IGNORE → ON CONFLICT DO NOTHING, строки dict-ом (row['col'])."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        if re.search(r"(?i)insert\\s+or\\s+ignore\\s+into", sql):
            sql = re.sub(r"(?i)insert\\s+or\\s+ignore\\s+into", "INSERT INTO", sql)
            sql += " ON CONFLICT DO NOTHING"
        sql = sql.replace("%", "%%").replace("?", "%s")
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return _Cur(cur)

    def executescript(self, _sql):
        raise NotImplementedError("Схема Postgres — в 01_schema_postgres.sql, init_db не нужен")


@contextmanager
def _db():
    """Соединение под ТЕКУЩИЙ салон: SET app.tenant_id, commit при успехе."""
    tid = _tenant.get()
    if tid is None:
        raise RuntimeError("tenant не задан: вызови set_tenant(<id>) до работы с БД")
    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as c:
            # SET LOCAL — действует только в этой транзакции (безопасно для пула
            # соединений: не протекает на следующего арендатора соединения).
            c.execute("SET LOCAL app.tenant_id = %s", (str(tid),))
        yield _Conn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()'''

# ── ручные SQL-правки (точные подстроки; количество печатается при прогоне) ──
FIXES = [
    # applogin_nonces: OR REPLACE → ON CONFLICT (nonce); семантика REPLACE
    # (полная перезапись строки: chat_id/token/consumed сбрасываются) сохранена.
    ('"INSERT OR REPLACE INTO applogin_nonces "\n'
     '            "(nonce, created_at, chat_id, token, consumed) VALUES (?, ?, NULL, NULL, 0)",',
     '"INSERT INTO applogin_nonces "\n'
     '            "(nonce, created_at, chat_id, token, consumed) VALUES (?, ?, NULL, NULL, 0) "\n'
     '            "ON CONFLICT (nonce) DO UPDATE SET created_at=excluded.created_at, "\n'
     '            "  chat_id=NULL, token=NULL, consumed=0",'),
    # notify_prefs: OR REPLACE → ON CONFLICT (client_id) DO UPDATE
    ('"INSERT OR REPLACE INTO notify_prefs (client_id, prefs, updated_at) "\n'
     '            "VALUES (?, ?, ?)",',
     '"INSERT INTO notify_prefs (client_id, prefs, updated_at) "\n'
     '            "VALUES (?, ?, ?) "\n'
     '            "ON CONFLICT (client_id) DO UPDATE SET "\n'
     '            "  prefs=excluded.prefs, updated_at=excluded.updated_at",'),
    # client_marketing_last: OR REPLACE → ON CONFLICT (client_id) DO UPDATE
    ('"INSERT OR REPLACE INTO client_marketing_last (client_id, sent_at) "\n'
     '            "VALUES (?, ?)",',
     '"INSERT INTO client_marketing_last (client_id, sent_at) "\n'
     '            "VALUES (?, ?) "\n'
     '            "ON CONFLICT (client_id) DO UPDATE SET sent_at=excluded.sent_at",'),
    # web_sessions: OR REPLACE → ON CONFLICT (token) DO UPDATE
    # (2026-07-03: вставка обзавелась tg_first_name/tg_last_name/tg_username/tg_photo_url)
    ('"INSERT OR REPLACE INTO web_sessions "\n'
     '            "(token, subject_kind, chat_id, phone_hash, vk_user_id, display_name, "\n'
     '            " tg_first_name, tg_last_name, tg_username, tg_photo_url, "\n'
     '            " created_at, expires_at, last_seen_at, revoked) "\n'
     '            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",',
     '"INSERT INTO web_sessions "\n'
     '            "(token, subject_kind, chat_id, phone_hash, vk_user_id, display_name, "\n'
     '            " tg_first_name, tg_last_name, tg_username, tg_photo_url, "\n'
     '            " created_at, expires_at, last_seen_at, revoked) "\n'
     '            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "\n'
     '            "ON CONFLICT (token) DO UPDATE SET "\n'
     '            "  subject_kind=excluded.subject_kind, chat_id=excluded.chat_id, "\n'
     '            "  phone_hash=excluded.phone_hash, vk_user_id=excluded.vk_user_id, "\n'
     '            "  display_name=excluded.display_name, "\n'
     '            "  tg_first_name=excluded.tg_first_name, tg_last_name=excluded.tg_last_name, "\n'
     '            "  tg_username=excluded.tg_username, tg_photo_url=excluded.tg_photo_url, "\n'
     '            "  created_at=excluded.created_at, "\n'
     '            "  expires_at=excluded.expires_at, last_seen_at=excluded.last_seen_at, "\n'
     '            "  revoked=excluded.revoked",'),
    # client_history_cache: OR REPLACE → ON CONFLICT (tenant_id, client_id) DO UPDATE
    ('"INSERT OR REPLACE INTO client_history_cache "\n'
     '            "(client_id, history_json, updated_at) VALUES (?, ?, ?)",',
     '"INSERT INTO client_history_cache "\n'
     '            "(client_id, history_json, updated_at) VALUES (?, ?, ?) "\n'
     '            "ON CONFLICT (tenant_id, client_id) DO UPDATE SET "\n'
     '            "  history_json=excluded.history_json, updated_at=excluded.updated_at",'),
    # record_state: ON CONFLICT target + tenant_id
    ('"ON CONFLICT(record_id) DO UPDATE SET "',
     '"ON CONFLICT (tenant_id, record_id) DO UPDATE SET "'),
    # settings: ON CONFLICT target + tenant_id
    ('"ON CONFLICT(key) DO UPDATE SET value = excluded.value, "',
     '"ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value, "'),
    # cutmatch_usage: ON CONFLICT target + tenant_id; count квалифицируем именем
    # таблицы — иначе Postgres ругается «column reference count is ambiguous».
    ('"ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",',
     '"ON CONFLICT (tenant_id, user_id, day) DO UPDATE SET count = cutmatch_usage.count + 1",'),
    # client_chat_state: MAX(a,b) — в SQLite скалярный max, в Postgres → GREATEST
    ('"booking_intent = MAX(COALESCE(booking_intent, 0), ?) "',
     '"booking_intent = GREATEST(COALESCE(booking_intent, 0), ?) "'),
]


def main():
    src = open(SRC, encoding="utf-8").read()
    assert "import sqlite3" in src
    src = src.replace("import sqlite3", NEW_LAYER, 1)
    assert OLD_DB in src, "не нашёл блок _db() — проверь database.py"
    src = src.replace(OLD_DB, NEW_DB, 1)
    src = src.replace("sqlite3.IntegrityError", "psycopg2.IntegrityError")
    src = src.replace("sqlite3.OperationalError", "psycopg2.OperationalError")
    # database.py сам вызывает init_db() на импорте (создаёт sqlite-таблицы).
    # Для Postgres схема — в 01_schema_postgres.sql, авто-вызов отключаем.
    src = re.sub(r"(?m)^init_db\(\)\s*$",
                 "# init_db() отключён для Postgres (схема в 01_schema_postgres.sql)", src)

    applied = 0
    for old, new in FIXES:
        if old in src:
            src = src.replace(old, new, 1); applied += 1
        else:
            print(f"⚠️ НЕ найден фрагмент для правки:\n{old[:70]}…")

    header = ("# АВТО-СГЕНЕРИРОВАНО port_database.py из боевого database.py.\n"
              "# Не править руками — менять database.py/порт и пересобирать.\n"
              "# Слой Postgres+tenant; init_db не используется (схема в 01_schema_postgres.sql).\n\n")
    open(OUT, "w", encoding="utf-8").write(header + src)

    leftover = len(re.findall(r"sqlite3\.[A-Za-z]", src))  # без учёта комментариев
    print(f"SQL-правок применено: {applied}/{len(FIXES)}")
    print(f"остаточных 'sqlite3.X' (д.б. 0, кроме комментария): {leftover}")
    print(f"строк в db_pg_full.py: {src.count(chr(10))}")
    print(f"записано: {OUT}")


if __name__ == "__main__":
    main()
