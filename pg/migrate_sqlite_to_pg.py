#!/usr/bin/env python3
"""
SaaS Этап 0 — Шаг 2: перенос данных SQLite → PostgreSQL как tenant_id=1.

Репетиция на КОПИИ боевой базы (barbershop_copy.db) → локальный Postgres saas_test.
Прод не трогается. Логика портируется и на Yandex Managed PG (поменять PG_*).

Что делает:
  * читает все строки каждой таблицы из SQLite;
  * вставляет в Postgres, добавив tenant_id=1 (id сохраняются — связи целы);
  * порядок: clients и referrals первыми (на них ссылаются FK);
  * после загрузки двигает счётчики IDENTITY на max(id), чтобы новые вставки
    не столкнулись со старыми id;
  * сверяет число строк SQLite vs Postgres по каждой таблице.
"""
import sqlite3, sys, psycopg2

SQLITE = __file__.rsplit("/", 1)[0].replace("Desktop/сайт и приложение/ai администратор/saas_blueprint/pg", "") \
         if False else "/Users/stanislavmosin/saas_pg_rehearsal/barbershop_copy.db"
PG = dict(host="/Users/stanislavmosin/saas_pg_rehearsal/pgdata", port=54329,
          user="postgres", dbname="saas_test")
TENANT_ID = 1
# FK-зависимости: эти таблицы грузим первыми, остальные — следом.
FIRST = ["clients", "referrals"]


def sqlite_tables(cur):
    rows = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def cols_of(scur, table):
    return [r[1] for r in scur.execute(f"PRAGMA table_info({table})").fetchall()]


def main():
    sconn = sqlite3.connect(SQLITE)
    scur = sconn.cursor()
    pconn = psycopg2.connect(**PG)
    pconn.autocommit = False
    pcur = pconn.cursor()

    # суперпользователь postgres обходит RLS — загрузка идёт чисто.
    all_tables = sqlite_tables(scur)
    order = FIRST + [t for t in all_tables if t not in FIRST]

    report = []
    for t in order:
        cols = cols_of(scur, t)
        rows = scur.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
        n_src = len(rows)
        if n_src:
            collist = ", ".join(["tenant_id"] + cols)
            ph = ", ".join(["%s"] * (len(cols) + 1))
            sql = f'INSERT INTO {t} ({collist}) VALUES ({ph})'
            data = [(TENANT_ID, *row) for row in rows]
            psycopg2.extras.execute_batch(pcur, sql, data, page_size=500) \
                if hasattr(psycopg2, "extras") else pcur.executemany(sql, data)
        report.append((t, n_src))
    pconn.commit()

    # сдвинуть счётчики IDENTITY на max(id)
    pcur.execute("""SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema='public' AND is_identity='YES'""")
    for tbl, col in pcur.fetchall():
        pcur.execute(
            f"SELECT setval(pg_get_serial_sequence('{tbl}','{col}'), "
            f"GREATEST((SELECT COALESCE(MAX({col}),1) FROM {tbl}), 1))")
    pconn.commit()

    # сверка строк
    print(f"{'таблица':<24}{'SQLite':>9}{'Postgres':>10}  ok")
    print("-" * 50)
    ok_all = True
    for t, n_src in report:
        pcur.execute(f"SELECT count(*) FROM {t}")
        n_pg = pcur.fetchone()[0]
        ok = (n_src == n_pg)
        ok_all = ok_all and ok
        print(f"{t:<24}{n_src:>9}{n_pg:>10}  {'✓' if ok else '✗ РАСХОЖДЕНИЕ'}")
    print("-" * 50)
    total_src = sum(n for _, n in report)
    pcur.execute("SELECT count(*) FROM tenants")
    print(f"строк перенесено: {total_src} | тенантов в реестре: {pcur.fetchone()[0]}")
    print("ИТОГ:", "✓ всё совпало" if ok_all else "✗ есть расхождения")
    pconn.close(); sconn.close()
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    import psycopg2.extras  # noqa
    main()
