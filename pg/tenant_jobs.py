"""
SaaS Шаг 5 — фоновые джобы по ВСЕМ салонам.

Сейчас в боте джобы глобальные (напоминания, дневной отчёт, лояльность,
реактивация, дни рождения, отзывы…). В мультитенанте каждую надо прогнать
ОТДЕЛЬНО по каждому салону, в его контексте, чтобы:
  • RLS показывал только данные этого салона;
  • ошибка одного салона не валила джобу для остальных;
  • каждый салон считался по СВОЕЙ конфигурации (часовой пояс, токены, % ЗП…).

Боевое применение: оборачиваем тело существующих *_job так:
    def _reminder_job():
        tenant_jobs.run_for_all_tenants(_remind_one_tenant, label="reminders")
где _remind_one_tenant(tid) использует обычные db_pg-функции (они уже scoped).
"""
from __future__ import annotations
import tenant_resolver as tr
import db_pg_full as db


def run_for_all_tenants(job_fn, label: str = "job") -> list[dict]:
    """Прогнать job_fn(tenant_id) для каждого активного салона в его tenant_scope.
    Возвращает [{tenant_id, slug, ok, result|error}]. Сбой одного — не трогает других."""
    out = []
    for t in tr.active_tenants():
        tid = t["id"]
        try:
            with db.tenant_scope(tid):
                res = job_fn(tid)
            out.append({"tenant_id": tid, "slug": t["slug"], "ok": True, "result": res})
        except Exception as e:
            out.append({"tenant_id": tid, "slug": t["slug"], "ok": False, "error": str(e)})
    return out


# ── Пример пер-тенантной джобы (демо «здоровье салона») ─────────────────────
# Внутри tenant_scope любые db._db()/db.<func> автоматически видят ТОЛЬКО свой
# салон (RLS) — джобе не нужно ничего знать про tenant_id.
def tenant_health(tenant_id: int) -> dict:
    with db._db() as c:
        clients = c.execute("SELECT count(*) AS n FROM clients").fetchone()["n"]
        pending = c.execute(
            "SELECT count(*) AS n FROM review_requests WHERE status = 'pending'"
        ).fetchone()["n"]
        upcoming = c.execute(
            "SELECT count(*) AS n FROM slot_waitlist WHERE notified_at IS NULL"
        ).fetchone()["n"]
    return {"clients": clients, "pending_reviews": pending, "waitlist": upcoming}
