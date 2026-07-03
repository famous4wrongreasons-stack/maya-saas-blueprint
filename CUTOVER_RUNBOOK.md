# Боевой катовер: перевод на мультитенантный Postgres-стек

Это план перевода БОЕВОГО салона на новый слой. Делается ОСОЗНАННО и фазами:
сначала переносим текущий салон на Postgres (риск минимальный, поведение 1:1),
проверяем, и только потом включаем подключение НОВЫХ салонов.

Всё в песочнице (`saas_blueprint/pg/`) уже проверено локально: ~98 тестов зелёные
(схема, миграция 1:1, слой данных, резолвинг, аудит изоляции, джобы, онбординг,
биллинг, white-label). Прод (SQLite) пока не тронут.

🔴 Главный принцип: **SQLite остаётся рабочим до самого переключения.** Откат =
вернуть один импорт и перезапустить сервис.

---

## ФАЗА 0 — white-label вживую БЕЗ Postgres (добавлено 2026-07-03)

Цель: доказать конвейер бренда (Шаги 1–4, все тесты зелёные) на проде для самой
МЭ, не мигрируя данные и не меняя поведение. Всё обратимо за минуты, боевой
`app/index.html` не трогается вообще.

Зачем: обкатать в реальном PWA механику «бренд из /api/tenant-config» до
большого катовера. МЭ увидит свой же бренд → изменений ноль, риск ноль.

### 0.1. VPS — эндпоинт в mock-режиме (2 строки + файл + env)

1. Залить на VPS (`/home/botadmin/barbershop-bot/`):
   `saas_blueprint/pg/tenant_config_api.py` и создать `tenant_me.json`:
   ```json
   {"slug": "malesthetic",
    "brand": {"name": "Мужская Эстетика", "city": "Ставрополь",
              "address": "ул. Лермонтова, 343", "phone": "+7 (962) 447-67-47"},
    "active": true}
   ```
2. В `webhook_server.py` сразу после `web_app = web.Application(...)` — 3 строки
   (env через setdefault, systemd-юнит НЕ трогаем):
   ```python
   import os as _tc_os, tenant_config_api as _tc_api
   _tc_os.environ.setdefault("MAYA_TENANT_CONFIG_MOCK", "/home/botadmin/barbershop-bot/tenant_me.json")
   _tc_api.attach(web_app)
   ```
   ⚠ mock-режим НЕ импортирует tenant_config/psycopg2 — Postgres не нужен.
3. `python3 -m py_compile webhook_server.py tenant_config_api.py` → restart.
4. Проверка: `curl -s http://127.0.0.1:8080/api/tenant-config | head` → JSON с брендом.
   Бэкап: `webhook_server.py.phase0.bak` лежит рядом.

### 0.2. Beget — проброс ОТДЕЛЬНЫМ файлом (общий api-proxy.php НЕ трогаем)

Фронт НЕ ходит на same-origin `/api/*` — только через PHP-прокси. Чтобы не
касаться боевого `api-proxy.php` (ошибка в нём = падают все API живого PWA),
кладём НОВЫЙ файл `.../public_html/app/tenant-config.php`:
```php
<?php
// Фаза 0 white-label: публичный бренд-конфиг с VPS. Нет ПД, нет секретов.
header('Content-Type: application/json; charset=utf-8');
$qh = isset($_GET['host']) ? ('?host=' . rawurlencode($_GET['host'])) : '';
$ch = curl_init('https://rt.malesthetic.pro/api/tenant-config' . $qh);
// ⚠ прямой :8080 закрыт фаерволом; боевой путь Beget→VPS = https://rt.malesthetic.pro
//   (nginx на VPS, извне отдаёт 403 — пускает только Beget). См. tg-config.php: bot_api_base.
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => ['Accept: application/json',
                           'X-Forwarded-Host: ' . ($_SERVER['HTTP_HOST'] ?? '')],
    CURLOPT_TIMEOUT => 8,
    CURLOPT_CONNECTTIMEOUT => 4,
]);
$body = curl_exec($ch); $err = curl_error($ch); curl_close($ch);
if ($err || !$body) { http_response_code(502); echo '{"error":"unavailable"}'; exit; }
header('Cache-Control: public, max-age=300');
echo $body;
```
Boot-скрипт фронта пробует цепочку: `/api/tenant-config` →
`/app/tenant-config.php?host=` → `/app/api-proxy.php?action=tenant_config...`
(последний вариант — на будущее, если в Фазе B решим слить в общий прокси).
Бэкенд читает X-Forwarded-Host → ?host → Host.

### 0.3. Beget — тестовый фронт ОТДЕЛЬНЫМ файлом

1. Перегенерировать из свежего прода: 
   `python3 saas_blueprint/pg/tenantize_frontend.py "<путь>/app.html"`
2. Залить `app-tenant.html` как `.../public_html/app/tenant-test.html`
   (боевой `app/index.html` НЕ трогать).
3. Открыть `https://malesthetic.pro/app/tenant-test.html` — приложение выглядит
   ровно как боевое (бренд тот же, это и есть тест), в DevTools видно
   `me_tenant_cfg_v1` в localStorage.
4. Смока-тест чужого бренда: подменить `tenant_me.json` на «Гриву» (из
   `tenant_config_demo.json`) → перезагрузить страницу → шапка/заголовок/контакты
   сменились. Вернуть `tenant_me.json` обратно.

### 0.4. Откат Фазы 0
- VPS: убрать 2 строки attach + env из юнита, restart. 
- Beget: удалить `tenant-test.html` и `tenant-config.php` (оба — отдельные файлы).
- Фронт-кэш: ключ `me_tenant_cfg_v1` сам перестанет обновляться; боевой
  `index.html` его вообще не читает.

Критерий выхода из Фазы 0: `tenant-test.html` неделю живёт без жалоб, смока
«Грива» проходит. После этого — ФАЗА A (Postgres) по плану ниже, а гейтинг
(`tenant_gate.attach` + middleware) включается только вместе с ФАЗОЙ B.

---

## ФАЗА A — текущий салон на Postgres (single-tenant, поведение 1:1)

Цель: тот же один салон работает как раньше, но на Postgres как `tenant_id=1`.
Мультитенантность ещё НЕ включаем — минимизируем риск.

### A1. Поднять БД (твой шаг в облаке)
1. Yandex Cloud → Managed Service for PostgreSQL 16, регион РФ (152-ФЗ).
2. Создать БД `barbershop`, пользователя-владельца.
3. Сохранить host/port/dbname/пароль — положить в окружение сервиса (НЕ в код).

### A2. Применить схему
```bash
psql "$PG_DSN" -f 01_schema_postgres.sql
psql "$PG_DSN" -f 02_tenant_resolution.sql
# роль приложения (НЕ суперпользователь — иначе RLS не действует):
psql "$PG_DSN" -c "CREATE ROLE salon_app LOGIN PASSWORD '...' NOSUPERUSER;
  GRANT USAGE ON SCHEMA public TO salon_app;
  GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO salon_app;
  GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO salon_app;
  GRANT SELECT,INSERT,UPDATE,DELETE ON tg_chat_binding, bot_registry TO salon_app;"
```

### A3. Перенести данные
1. Снять консистентный снимок боевой SQLite (backup-API, не мешает боту):
   `python -c "import sqlite3; s=sqlite3.connect('barbershop.db'); d=sqlite3.connect('/tmp/snap.db'); s.backup(d)"`
2. Прогнать `migrate_sqlite_to_pg.py` (поменять PG=… на Yandex DSN, SQLITE=… на снимок).
   Сверить: число строк по каждой из 31 таблицы должно совпасть (скрипт это печатает).

### A4. Подключить код к Postgres (на сервере)
1. **Бэкап:** `cp database.py database.py.sqlite.bak`.
2. Залить `db_pg_full.py` и переименовать в `database.py` (он — порт database.py,
   API совпадает). В нём:
   - `PG = {...}` → читать из окружения (host/port/user=salon_app/password/dbname);
   - поставить `psycopg2-binary` в venv.
3. **Контекст салона.** Пока single-tenant — проще всего на старте сервиса один раз
   `database.set_tenant(1)` (и в фоновых джобах тоже). Точки входа НЕ переписываем.
4. `pii_crypto` ключ и (для будущего онбординга) `SAAS_SECRET_KEY` — в окружении.

### A5. Проверка и переключение
1. Прогнать сервис на staging/в отдельном процессе, указывающем на Postgres.
2. Дымовой регресс: запись/перенос/отмена, оплата визита, баллы, отчёт, журнал,
   чат Антона — всё как на SQLite.
3. Переключить боевой сервис, `systemctl restart barbershop-bot`. Наблюдать логи.

### A6. Откат (если что-то не так)
`cp database.py.sqlite.bak database.py && systemctl restart barbershop-bot` —
мгновенно вернулись на SQLite (данные там не трогались).

**Критерий выхода из Фазы A:** салон неделю стабильно работает на Postgres.

---

## ФАЗА B — включить мультитенантность (продавать новым салонам)

🔴 ЧЕКЛИСТ ПЕРЕД САЛОНОМ №2 (найдено при перегенерации схемы 2026-07-03):
инлайновые natural-key PK не переведены на (tenant_id, col) — для одного салона
корректно, для второго дадут коллизии client_id между салонами. Перевести PK
на композитные минимум у: notify_prefs, client_marketing_last, masters_telegram,
settings, record_state, processed_records (и пересмотреть остальные инлайн-PK
в 01_schema_postgres.sql). applogin_nonces (nonce) и web_sessions (token) —
случайные глобальные ключи, оставить как есть (по nonce/token ищут БЕЗ тенанта).

Только после стабильной Фазы A.

### B1. Per-tenant YClients
🔴 Боевой `yclients.YClientsAPI` читает креды из глобального config. Дать
конструктору принимать `company_id/user_token/cash_account_id/cashless_account_id`
(для салона 1 — дефолт из config, чтобы не сломать). Тогда
`booking_provider.YClientsProvider` заработает на любой салон.

### B2. Точки входа через резолвер
- Telegram: обернуть обработчик в `saas_runtime.telegram_request(chat, payload, token)`
  (резолвит салон → `tenant_scope`). На общем боте — `?start=<slug>` привязывает чат.
- HTTP/PWA: middleware `saas_runtime.http_request(host, session_tenant_id)`; салон —
  из АВТОРИЗОВАННОЙ web-сессии, Host — только лендинг.
- Фич-роуты: `require_active=True` (биллинг-гейт); роуты оплаты/лендинга — без него.

### B3. Джобы по тенантам
Тело каждой `*_job` обернуть в `tenant_jobs.run_for_all_tenants(<job>, ...)`.
Добавить джобы биллинга: `tenant_billing.enforce_access(now())` (статусы) и
`charge_due(YooKassaGateway(...), now())` (рекуррент).

### B4. Поддомены + white-label
- DNS wildcard `*.app.ru` → сервер; эндпоинт `/api/tenant-config` =
  `tenant_config.public_config(request.host)`; index.html на старте применяет бренд
  (см. `whitelabel_boot_example.js`).
- Боты: общий бот-платформа (deeplink по slug) на старте; выделенные боты
  (`bot_registry`) — как платная white-label опция.

### B5. Онбординг-флоу
Лендинг → регистрация (`tenant_onboarding.create_tenant`) → «Подключить YClients»
(`connect_yclients` → `sync_catalog`) → выбор тарифа + триал → оплата
(`tenant_billing.activate_paid` из YooKassa-вебхука). Маркетплейс YClients —
полировка (1-клик подключение) уже после первых ручных подключений.

---

## Что остаётся «бумажной» работой (юр/орг, не код)
- 152-ФЗ: ты становишься ОБРАБОТЧИКОМ ПД для каждого салона → оферта + поручение
  на обработку. Данные уже в РФ — плюс.
- Партнёрка YClients: уточнить лимиты partner-токена на много компаний и условия
  публикации в Маркетплейсе.
- Тарифы/цены: значения в `tenant_billing.PLANS` — поставить реальные.

## Порядок, если коротко
A1→A2→A3→A4→A5 (живём неделю) → B1→B2→B3→B4→B5 → первый внешний салон.
