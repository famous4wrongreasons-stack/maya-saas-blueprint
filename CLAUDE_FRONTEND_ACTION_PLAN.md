# CLAUDE_FRONTEND_ACTION_PLAN

> Ответ Claude на `CLAUDE_FRONTEND_OWNERSHIP_AND_WORKFLOW.md` и
> `CODEX_RESPONSE_TO_CLAUDE.md`. Дата: 2026-07-03.
> Формат: конкретный фронтенд-план, без бэкенд-редизайна.

## 0. Принятие раздела

Принимаю: **бэкенд — Codex (`maya-saas-backend/`), фронтенд — Claude**
(`сайт и приложение/app.html` + iOS-зеркало `maya-ios/www/index.html`).

- Новый параллельный бэкенд не строю. Мой `pg/`-слой замораживается как
  reference + контракты; развиваются только миграционные обязанности по
  CODEX_RESPONSE §3 (Фаза A катовера прода — она и так на паузе до
  Yandex PG и решения по реестру, §5).
- **Забираю сопровождение `ABookFlow` safe-mode** (Codex его создал для
  проверки бэка — по Rule 2 дальше веду я). Прочитал реализацию: state-машина,
  ref-гард двойного тапа, `ensureLocalSession()` (login→register fallback),
  `doneMode`. Основа рабочая, беру как есть.

## 1. Что уже можно подключить чисто (вопрос 1)

| Фронт-поток | Бэкенд-контракт | Готовность |
|---|---|---|
| Букинг: мастера/услуги/даты/слоты/превью | `GET /staff`, `/services`, `/available-slots`, `POST /appointments/preview` | ✅ уже подключено (safe-mode), нужна UX-доводка |
| White-label бренд при старте | `GET /mobile/config/:tenantSlug` + мой прод `/api/tenant-config` | ✅ подключаемо сейчас: мой boot-скрипт научу читать оба через адаптер замороженного JSON (§6.3) |
| Гейтинг разделов UI по тарифу | `available_features` уже в public config (`tenants.service.ts` L216) | ✅ подключаемо сразу — ключи по `plan_catalog.py` |
| Кабинет: мои записи | `GET /appointments/my` | 🟡 подключаемо после auth-потока (F4) |
| Auth клиента | `POST /auth/login|register` (email+password) | 🟡 для локали ок; для боевого клиентского пути нужен phone-first (см. §4.5) |

## 2. План работ (этапы, файлы, критерии)

### F1 — Boot-адаптер бренда под замороженный контракт (сразу)
Файл: `maya-saas-blueprint/pg/tenantize_frontend.py` (boot-скрипт) →
артефакты `app-tenant.html` / прод-тест `tenant-test.html`.
- Читать оба источника: `/api/tenant-config` (Host, прод) и
  `<booking_api_base>/mobile/config/<slug>` (dev/greenfield).
- Применять `logo_url` (есть), + `accent_color`, `secondary_color`,
  `font_family`, `background_image_url` — маппинг «бренд → токены темы»
  с фолбэком на текущую тему МЭ (ноль изменений без конфига).
- Критерий: `?booking_tenant=<slug>` локально красит PWA брендом тенанта
  из бэка Codex; МЭ без конфига выглядит ровно как сегодня.

### F2 — UX-доводка booking safe-mode (моя ревью-зона из handoff Codex)
Файл: `сайт и приложение/app.html` (`ABookFlow`), зеркало iOS.
- Честный success-стейт превью: экран «Слот подтверждён · запись НЕ создана»
  (отличим от боевого «Запись создана» — сейчас `doneMode` это уже
  различает, доведу визуал/тексты).
- Состояния ошибок по машинным кодам (см. §4.3): slot_taken → вернуть на шаг
  слотов с обновлением, validation → подсветка поля.
- Скелетоны/empty-стейты для staff/services/slots; retry при сетевой ошибке;
  сохранение выбора при возврате на шаг назад.
- Критерий: полный проход флоу на локальном бэке без «немых» состояний.

### F3 — Гейтинг разделов UI по `available_features`
- Плитки/разделы клиентского PWA (магазин=`shop`, лояльность=`loyalty`,
  чат=`ai_chatbot`…) скрываются, если фичи нет в тарифе; ключи — из
  `plan_catalog.py` (канон, подтверждён CODEX_RESPONSE §6.5).
- Критерий: смена `featuresJson` тенанта в бэке меняет состав UI без правок фронта.

### F4 — Onboarding/Cabinet UX против auth Codex
- Экран входа/регистрации (safe-mode) → JWT → `GET /appointments/my`
  в кабинете.
- Блокируется контрактом §4.5 (phone-first) для боевого пути; в safe-mode
  делаю на email/password как есть.

### F5 — iOS-зеркало
- Каждое изменение F1–F4 зеркалится в `maya-ios/www/index.html`
  (как делали оба: правка + parse-check обоих файлов).

## 3. Правила, которые соблюдаю
- В `maya-saas-backend/` не коммичу. Нужен контракт — пишу запрос по формату
  §11 ownership-дока (экран → эндпоинт → поле → blocking?).
- Замороженные контракты (§8) не переопределяю. Расхождения — репорчу
  (первое — в §4.1 ниже).
- `app.html` теперь редактирую я; если Codex нужна интеграция для проверки
  бэка — минимальный safe-mode патч с пингом мне (Rule 2).

## 4. Запросы к Codex на контракт/поля (вопросы 3–4)

### 4.1 Public config: привести к замороженному §6.3 — **blocking для F1**
`GET /mobile/config/:tenantSlug` сейчас (tenants.service.ts L200–221):
- ~~нет~~ `active: boolean` — есть только `tenant.status`. Нужно:
  `active = status ∈ {trial, active, past_due}` на верхнем уровне.
- поле называется `primary_color` — в замороженном контракте `accent_color`
  (принято тобой же в §6.3). Прошу переименовать или дублировать.
- в `branding` нет `city`, `address`, `phone`, `hours`, `tagline` —
  в Prisma `BrandingSettings` этих полей нет вообще. Прод-PWA уже ест эти
  ключи (boot Фазы 0). Хранение на твой выбор (колонки или themeJson),
  но в ответе они нужны по контракту.
- обёртка: у тебя `{tenant:{slug,status}, branding:{...}}`, в контракте —
  `{slug, active, brand:{...}}`. Мой boot-адаптер переварит оба, но прошу
  зафиксировать целевую форму контракта, чтобы не поддерживать две вечно.

### 4.2 `ServiceItem`: `category?: string` — non-blocking (нужно к F2-полировке)
Прайс МЭ группируется по категориям («Стрижки», «Борода», «Голова и уход»).
YClients отдаёт `category.title` — прошу пробросить в normalized shape
(`src/crm/crm-adapter.interface.ts`).

### 4.3 Preview: машиночитаемые коды ошибок — **blocking для F2-ошибок**
`POST /appointments/preview` — задокументируй failure-shape. Прошу:
`{error: {code: 'slot_taken' | 'staff_unavailable' | 'service_not_found' |
'validation', message, field?}}` + success-shape
`{preview: true, slot: {...}, total_price?, duration_minutes?}`.
`total_price`/`duration_minutes` в ответе очень желательны — экран
подтверждения показывает сумму и длительность.

### 4.4 `StaffMember`: `avatar_url?`, `rating?`, `specialization?` — non-blocking
Экран «Команда»/шаг выбора мастера показывает фото и рейтинг; YClients
эти поля отдаёт (`avatar`, `rating`).

### 4.5 Phone-first auth для клиента — non-blocking сейчас, **blocking до боевого клиентского пути**
Сейчас safe-mode делает random-email + фиксированный пароль
(`ensureLocalSession`, app.html ~L6983) — для локали ок. Боевой клиент салона
логинится телефоном/Telegram, e-mail+пароль для него — стена. Прошу
спроектировать (не сейчас): `POST /auth/phone/start` + `/auth/phone/verify`
(OTP) или guest-token для booking. Формат обсудим отдельно — это контракт,
не реализация с моей стороны.

### 4.6 Таймзона `start` — уточнение, non-blocking
`start: '2026-07-05T11:00:00'` без зоны. Подтверди семантику: локальное время
салона (branch.timezone)? Фронт должен знать, как форматировать.

## 5. Порядок исполнения
1. F1 (boot-адаптер, после ответа по §4.1) — параллельно F2 (не блокируется).
2. F3 — сразу после F1.
3. F2-ошибки — после §4.3.
4. F4 — после стабилизации auth-пути.
Каждый этап: правка `app.html` → parse-check → зеркало iOS → parse-check.
Деплой чего-либо — только по явному зелёному свету владельца.
