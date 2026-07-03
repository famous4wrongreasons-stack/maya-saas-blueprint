# -*- coding: utf-8 -*-
"""Тесты tenant_config_api: mock-режим, парсинг Host, отсутствие конфига.
Запуск: python3 test_tenant_config_api.py  (или pytest)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tenant_config_api as api  # noqa: E402


def test_host_only():
    assert api._host_only("demo.app.ru:443") == "demo.app.ru"
    assert api._host_only("Demo.App.RU") == "demo.app.ru"
    assert api._host_only(None) == ""
    assert api._host_only("") == ""


def test_mock_mode():
    cfg = {"slug": "t1", "brand": {"name": "Салон X", "phone": "+7 (900) 000-00-00"},
           "active": True}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
        path = f.name
    os.environ[api.MOCK_ENV] = path
    try:
        got = api.resolve_payload("любой-хост.ру")
        assert got == cfg, got
        # битый mock-файл → None (фронт остаётся на дефолте)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        assert api.resolve_payload("x") is None
    finally:
        del os.environ[api.MOCK_ENV]
        os.unlink(path)


def test_no_config_no_pg():
    # без mock и без Postgres резолв не падает, а возвращает None
    assert os.environ.get(api.MOCK_ENV) is None
    assert api.resolve_payload("unknown.example") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("OK")
