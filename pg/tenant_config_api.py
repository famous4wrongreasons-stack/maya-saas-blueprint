"""
SaaS Шаг 1 — HTTP-обвязка публичного бренд-конфига: GET /api/tenant-config.

Модуль НАМЕРЕННО не подключён к боевому webhook_server.py. Когда Стас даст
зелёный свет, подключение = одна строка в setup-е приложения:

    import tenant_config_api
    tenant_config_api.attach(app)          # app — aiohttp.web.Application

Режимы работы (по убыванию приоритета):
  1. MOCK: env MAYA_TENANT_CONFIG_MOCK=/path/to/config.json — отдаёт этот JSON
     любому Host (локальная разработка/предпросмотр без PostgreSQL).
  2. PG:   tenant_config.public_config(Host) — боевой режим (нужен Postgres
     и tenant_resolver; это уже написано в этом же каталоге).
  3. Ничего не настроено → 404 {"error":"tenant_not_found"} — фронт остаётся
     на дефолтном бренде платформы (та же «Мужская Эстетика», что и сегодня).

Безопасность: наружу уходит ТОЛЬКО то, что вернул tenant_config.public_config
(белый список PUBLIC_BRAND_FIELDS) либо содержимое mock-файла. Никаких секретов
модуль не читает. Эндпоинт неаутентифицированный by design (бренд нужен до логина).
"""
from __future__ import annotations

import json
import os

MOCK_ENV = "MAYA_TENANT_CONFIG_MOCK"
CACHE_CONTROL = "public, max-age=300"  # бренд меняется редко; 5 мин достаточно


# ─────────────────────────── ядро (без веб-фреймворка) ───────────────────────────

def resolve_payload(host: str) -> dict | None:
    """Публичный конфиг для Host. None = салон не найден (фронт покажет дефолт)."""
    mock_path = os.environ.get(MOCK_ENV)
    if mock_path:
        try:
            with open(mock_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    try:
        import tenant_config  # локальный модуль blueprint (требует Postgres)
        return tenant_config.public_config(host)
    except Exception:
        return None


def _host_only(raw: str | None) -> str:
    """'demo.app.ru:443' → 'demo.app.ru'. Пустой/битый Host → ''."""
    if not raw:
        return ""
    return raw.split(":", 1)[0].strip().lower()


def pick_host(xfwd: str | None, host: str | None, query_host: str | None) -> str:
    """Исходный хост клиента. Фронт МЭ ходит через Beget-прокси (api-proxy.php),
    поэтому Host запроса = VPS; настоящий хост прокси передаёт заголовком
    X-Forwarded-Host или параметром ?host=. Приоритет: X-Forwarded-Host → ?host → Host."""
    return _host_only(xfwd) or _host_only(query_host) or _host_only(host)


# ─────────────────────────── aiohttp (боевой VPS) ───────────────────────────

def attach(app) -> None:
    """Добавить GET /api/tenant-config в существующее aiohttp-приложение."""
    from aiohttp import web

    async def handler(request):
        cfg = resolve_payload(pick_host(request.headers.get("X-Forwarded-Host"),
                                        request.headers.get("Host"),
                                        request.query.get("host")))
        if cfg is None:
            return web.json_response(
                {"error": "tenant_not_found"}, status=404,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(cfg, headers={"Cache-Control": CACHE_CONTROL})

    app.router.add_get("/api/tenant-config", handler)


# ──────────────────── stdlib dev-сервер (локально, без aiohttp) ────────────────────

def run_dev(port: int = 8125, mock_path: str | None = None) -> None:
    """Локальный мок-сервер: /api/tenant-config + статика из текущего каталога.
    Только для разработки — на VPS используется attach()."""
    if mock_path:
        os.environ[MOCK_ENV] = mock_path
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    class H(SimpleHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            if self.path.split("?", 1)[0] == "/api/tenant-config":
                cfg = resolve_payload(_host_only(self.headers.get("Host")))
                body = json.dumps(
                    cfg if cfg is not None else {"error": "tenant_not_found"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200 if cfg is not None else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", CACHE_CONTROL if cfg else "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *a):  # тихий режим
            pass

    print(f"tenant-config dev server → http://127.0.0.1:{port}/api/tenant-config "
          f"(mock={os.environ.get(MOCK_ENV) or '—'})")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Dev-сервер /api/tenant-config (mock)")
    p.add_argument("--mock", help="путь к JSON бренд-конфига", default=None)
    p.add_argument("--port", type=int, default=8125)
    a = p.parse_args()
    run_dev(port=a.port, mock_path=a.mock)
