# Аудит изоляции мультитенантности — отчёт (Шаг 4)

Проведён adversarial-аудит: 4 независимых аудитора (линзы: RLS/схема, слой данных,
резолвер, сквозные связки) + скептики-верификаторы. Часть находок подтверждена
**эмпирически на живом Postgres** (роль `salon_app`, NOSUPERUSER, FORCE RLS).

## 🔴 Подтверждённые HIGH — ИСПРАВЛЕНЫ и перепроверены

### 1. Cross-tenant FK: ссылка на клиента чужого салона
**Суть:** FK `client_id → clients(id)` был не-композитным. Проверка ссылочной
целостности в Postgres ОБХОДИТ RLS, поэтому салon A мог вставить бронь со ссылкой
на `client_id` салона B. Воспроизведено live: enumeration чужих id (existence
oracle), «отравление» строк, и блокировка удаления клиента владельцем
(`ERROR: ... violates foreign key`) — нарушение права на стирание (152-ФЗ).
**Фикс:** все 16 доменных FK сделаны КОМПОЗИТНЫМИ
`FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id)` +
`UNIQUE(tenant_id, id)` на родителях (clients, referrals). `generate.py` →
`_composite_fk()`. **Проверка:** `test_fk_isolation.py` — чужая ссылка теперь
даёт ForeignKeyViolation, своя проходит. ✓

### 2. Deeplink-перепривязка: угон чата по угадыванию id
**Суть:** `?start=salon_<N>` принимал последовательный id и БЕЗУСЛОВНО
перепривязывал чат к любому салону (жёг квоту/деньги салона B, ломал маршрутизацию
жертвы, давал keyed-PII чтение под B). Подтверждено в коде.
**Фикс (`tenant_resolver.py`):** (1) пейлоад трактуется ТОЛЬКО как slug (не id);
(2) привязка — только при ПЕРВОМ контакте, молчаливая смена салона запрещена.
Боевой апгрейд — подписанный invite-токен (HMAC+TTL). **Проверка:**
`test_tenant_resolver.py` — числовой salon_<id> не угоняет чат, смена заблокирована. ✓

## Прочее — статус

| Находка | Серьёзность | Статус |
|---|---|---|
| `SET` вместо `SET LOCAL` (небезопасно с пулом) | medium | ✅ ИСПРАВЛЕНО — `SET LOCAL` + `tenant_scope()` со сбросом contextvar |
| stale-tenant из contextvar между запросами | (critical-claim) | ✅ ЗАКРЫТО `tenant_scope()` (set+reset); правило: каждая точка входа оборачивает работу в него |
| `register_bot('')` схлопывает все боты в один хэш | medium | ✅ ИСПРАВЛЕНО — пустой токен → ValueError |
| `_tenant_from_payload` ValueError на unicode-цифрах | low | ✅ МООТ — числовая ветка удалена |
| `lastrowid` через session-wide `lastval()` | medium | ⚠️ ПРИНЯТО — все call-sites читают сразу после INSERT; долгосрочно → `RETURNING id` |
| `%`→`%%` в обёртке ломает будущий SQL с `%` (mod/date) | low | ⚠️ ПРИНЯТО — текущий код корректен; задокументировано |
| `web_sessions.token` глобальный PK | medium | ⚠️ ПРИНЯТО — токен 256-бит random; ON CONFLICT не меняет tenant_id; коллизия ничтожна |
| `tenant_for_host` доверяет заголовку Host | (high-claim) | 📌 К WIRING — пока нет вызовов; при подключении web-слоя салон брать из АВТОРИЗОВАННОЙ сессии (web_sessions.tenant_id), Host — только для лендинга |

## Вывод
Базовые механизмы (RLS USING/WITH CHECK, NOSUPERUSER-роль, DEFAULT tenant_id,
per-tenant uniqueness) работают корректно. Две структурные дыры (композитные FK и
deeplink) закрыты и перепроверены вживую. Остальное — принятые риски с
обоснованием или задачи на этап подключения web-слоя.
