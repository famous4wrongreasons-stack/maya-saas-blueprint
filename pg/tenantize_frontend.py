# -*- coding: utf-8 -*-
"""
SaaS Шаг 1 — «тенантизация» фронта БЕЗ форка.

Боевой app.html живёт и меняется, поэтому white-label выражен не правками
в проде, а этим детерминированным патчером:

    python3 tenantize_frontend.py /путь/к/app.html [-o app-tenant.html]

Что делает (и НИЧЕГО больше):
  1. Вставляет boot-скрипт сразу после блока window.APP_DATA: тот тянет
     GET /api/tenant-config и накладывает публичный бренд салона на APP_DATA
     ДО старта React (плюс кэш в localStorage для мгновенного второго визита).
     Нет конфига / любая ошибка → приложение выглядит ровно как сегодня.
  2. Заменяет 8 строк-«отщепенцев», где бренд захардкожен мимо APP_DATA
     (Wordmark, alt логотипов, превью виджетов кастомайзера, экран входа),
     на чтение из APP_DATA.brand с фолбэком на текущую строку.

Каждая замена заякорена и проверяется на ТОЧНОЕ число вхождений — если будущий
app.html «уехал», патчер падает с понятной ошибкой, а не молча портит файл.
Дизайн/стили/компоненты не затрагиваются вообще: меняются только источники строк.

После записи выходного файла все inline-скрипты прогоняются через `node --check`.
Исходный app.html не модифицируется никогда.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def uesc(t: str) -> str:
    """Кириллица → \\uXXXX, как пишет компилятор в JS-строках app.html."""
    return "".join("\\u%04X" % ord(c) if ord(c) > 127 else c for c in t)


NAME_RU = "Мужская Эстетика"
NAME_U = uesc(NAME_RU)

# Выражение «бренд из APP_DATA или прежний хардкод». fallback передаётся
# УЖЕ в кавычках нужного стиля, чтобы байты фолбэка совпадали с прежними.
def brand_expr(key: str, fallback_literal: str) -> str:
    return ("(window.APP_DATA&&window.APP_DATA.brand&&window.APP_DATA.brand."
            + key + "||" + fallback_literal + ")")


BOOT_SCRIPT = """<script>
/* ═══ MAYA SaaS Шаг 1: white-label boot (вставлен tenantize_frontend.py) ═══
   Накладывает публичный бренд салона (GET /api/tenant-config) на window.APP_DATA
   ДО старта React. Нет конфига или любая ошибка → приложение выглядит ровно как
   сегодня: каждая замена в коде имеет фолбэк на прежний бренд. */
(function () {
  var KEY = "me_tenant_cfg_v1";
  function apply(cfg) {
    try {
      if (!cfg || !cfg.brand) return;
      var b = cfg.brand;
      window.__TENANT_BRAND = b;
      var d = window.APP_DATA = window.APP_DATA || {};
      var br = d.brand = d.brand || {};
      ["name", "city", "address", "phone", "hours", "tagline"].forEach(function (k) {
        if (b[k]) br[k] = b[k];
      });
      if (b.logo_url) window.__ME_LOGO = b.logo_url;
      if (b.name) document.title = b.name;
      (d.contacts || []).forEach(function (row) {
        if (!row) return;
        if (row[0] === "phone" && b.phone) row[1] = b.phone;
        if (row[0] === "pin" && b.address) row[1] = b.address;
      });
      if (cfg.active === false) window.__TENANT_SUSPENDED = true;
    } catch (e) {}
  }
  /* 1) мгновенно — кэш прошлого визита (до парсинга React-бандла) */
  try { var c = localStorage.getItem(KEY); if (c) apply(JSON.parse(c)); } catch (e) {}
  /* 2) свежий конфиг; обычно успевает до маунта (1 МБ JS парсится дольше).
     Инфра МЭ: /api/* на Beget не проксируется — API ходит через
     /app/api-proxy.php?action=..., поэтому цепочка URL с фолбэком. */
  var URLS = [
    "/api/tenant-config",
    "/app/tenant-config.php?host=" + encodeURIComponent(location.host),
    "/app/api-proxy.php?action=tenant_config&host=" + encodeURIComponent(location.host)
  ];
  function pull(i) {
    if (i >= URLS.length) return;
    try {
      fetch(URLS[i], { credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw 0; return r.json(); })
        .then(function (cfg) {
          if (!cfg || !cfg.brand) throw 0;
          try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (e) {}
          apply(cfg);
        })
        .catch(function () { pull(i + 1); });
    } catch (e) { pull(i + 1); }
  }
  pull(0);
})();
</script>"""


def build_replacements() -> list[tuple[str, str, str, int]]:
    """(описание, искомое, замена, ожидаемое_число_вхождений)"""
    R: list[tuple[str, str, str, int]] = []

    # 1. alt у логотипа (LogoM ×2, \u-форма)
    old = 'alt: "' + NAME_U + '",'
    R.append(("alt логотипа (LogoM ×2)", old,
              "alt: " + brand_expr("name", '"' + NAME_U + '"') + ",", 2))

    # 2. Wordmark: текст-ребёнок "\uМужская Эстетика"
    old = '}, "' + NAME_U + '")'
    R.append(("Wordmark (\\u)", old,
              "}, " + brand_expr("name", '"' + NAME_U + '"') + ")", 1))

    # 2b. Экран входа: составная строка "Имя · Город" (\u-формы, разделитель \xB7)
    city_u = uesc("Ставрополь")
    old = '"' + NAME_U + ' \\xB7 ' + city_u + '"'
    R.append(("экран входа: имя · город (\\u)", old,
              brand_expr("name", '"' + NAME_U + '"')
              + ' + " \\xB7 " + '
              + brand_expr("city", '"' + city_u + '"'), 1))

    # 3. hwPreview «О нас»: имя салона в превью виджета
    old = 'marginTop: 8 } }, "' + NAME_RU + '")]'
    R.append(("hwPreview about: имя", old,
              'marginTop: 8 } }, ' + brand_expr("name", '"' + NAME_RU + '"') + ')]', 1))

    # 4. hwPreview «Контакты»: телефон
    old = '"8-962-447-67-47"'
    R.append(("hwPreview contacts: телефон", old,
              brand_expr("phone", '"8-962-447-67-47"'), 1))

    # 5. hwPreview «Контакты»: адрес
    old = '}, "Лермонтова, 343")]'
    R.append(("hwPreview contacts: адрес", old,
              "}, " + brand_expr("address", '"Лермонтова, 343"') + ")]", 1))

    # 6. alt логотипа в шапке (одинарные кавычки)
    old = "alt: '" + NAME_RU + "',"
    R.append(("alt логотипа (шапка)", old,
              "alt: " + brand_expr("name", "'" + NAME_RU + "'") + ",", 1))

    # 7. Экран смены входа: подпись под логотипом
    old = "}, '" + NAME_RU + "')"
    R.append(("экран входа: подпись", old,
              "}, " + brand_expr("name", "'" + NAME_RU + "'") + ")", 1))

    return R


def find_boot_insert_pos(s: str) -> int:
    """Позиция сразу после </script> блока, где определён window.APP_DATA."""
    i = s.index("window.APP_DATA = {")
    j = s.index("</script>", i) + len("</script>")
    return j


def node_check_all_scripts(html: str) -> list[str]:
    """`node --check` для каждого inline-скрипта. Возвращает список ошибок."""
    errors = []
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    with tempfile.TemporaryDirectory() as td:
        for i, b in enumerate(blocks):
            p = Path(td) / f"blk{i}.js"
            p.write_text(b, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(p)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                errors.append(f"скрипт #{i}: {r.stderr.strip()[:300]}")
    return errors, len(blocks)


def main() -> int:
    ap = argparse.ArgumentParser(description="app.html → app-tenant.html (white-label)")
    ap.add_argument("source", help="путь к боевому app.html (только чтение)")
    ap.add_argument("-o", "--out", default=None,
                    help="выходной файл (по умолчанию app-tenant.html рядом с исходным)")
    a = ap.parse_args()

    src = Path(a.source)
    out = Path(a.out) if a.out else src.with_name("app-tenant.html")
    s0 = src.read_text(encoding="utf-8")
    src_sha = hashlib.sha256(s0.encode()).hexdigest()[:16]
    s = s0

    # ── замены с жёсткой проверкой числа вхождений ──
    print(f"Источник: {src}  (sha256:{src_sha}, {len(s0):,} байт)")
    for desc, old, new, expect in build_replacements():
        n = s.count(old)
        if n != expect:
            # 2026-07-04: часть замен встроена прямо в исходник app.html
            # (safe-mode фикс входного экрана) — считаем применённой.
            if n == 0 and new in s:
                print(f"• {desc}: уже в исходнике — пропускаю")
                continue
            print(f"✗ СТОП: «{desc}» — найдено {n}, ожидалось {expect}.\n"
                  f"  app.html изменился; обновите якорь в tenantize_frontend.py.")
            return 1
        s = s.replace(old, new)
        print(f"✓ {desc}: {expect} замен(ы)")

    # ── вставка boot-скрипта после APP_DATA ──
    pos = find_boot_insert_pos(s)
    s = s[:pos] + "\n" + BOOT_SCRIPT + s[pos:]
    print("✓ boot-скрипт вставлен после window.APP_DATA")

    # ── верификация синтаксиса ──
    errs, nblocks = node_check_all_scripts(s)
    if errs:
        print(f"✗ node --check: {len(errs)} ошибок — файл НЕ записан")
        for e in errs:
            print("   " + e)
        return 1
    print(f"✓ node --check: все {nblocks} inline-скриптов валидны")

    # ── защита: исходник не изменён ──
    assert hashlib.sha256(src.read_text(encoding='utf-8').encode()).hexdigest()[:16] == src_sha

    out.write_text(s, encoding="utf-8")
    print(f"→ записан {out}  ({len(s):,} байт, Δ{len(s) - len(s0):+,})")
    print("Исходный app.html не изменён.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
