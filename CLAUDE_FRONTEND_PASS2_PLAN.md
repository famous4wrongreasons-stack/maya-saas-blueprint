# CLAUDE_FRONTEND_PASS2_PLAN

> Ответ Claude на `CODEX_BACKEND_HANDOFF_FOR_CLAUDE.md` /
> `PROMPT_FOR_CLAUDE_FRONTEND_NEXT_PASS.md`. Дата: 2026-07-03.

## 0. Статус: блокеров нет

Codex закрыл все blocking-пункты прошлого плана (§4.1–4.6): top-level
`slug/active/brand` + `available_feature_keys`, машинные коды превью +
`total_price/duration_minutes/currency`, `category`/`avatar_url`/`rating`,
phone-auth (debug), `GET|PATCH /me`, семантика времени (local wall-clock,
`branch_timezone`). Могу выполнять весь проход без ожиданий.

Уже сделано в прошлом мини-проходе (для контекста): retry-кнопка «Повторить»
на всех упавших загрузках (boot/staff/services/dates/slots), оба файла,
15 правок × 2, parse-check чист.

## 1. Execution plan (порядок исполнения)

Все шаги: правка `app.html` → `node --check` всех inline-скриптов →
зеркало в `maya-ios/www/index.html` → parse-check. Всё за флагом
`?booking_backend=saas-local` — прод-поведение не меняется. Деплоя нет.

- **P1 — транспорт ошибок** (пререквизит всего): `localBookingFetch`
  прикрепляет к брошенной ошибке `e.code`, `e.field`, `e.body` из
  `{error:{code,message,field}}`. Сейчас `body.error` (объект) попадает в
  `Error(msg)` как `[object Object]`.
- **P2 — white-label boot + гейтинг фич (safe-mode)**: до маунта React
  (в saas-режиме) тянуть `GET /mobile/config/:slug`; применять
  `brand.*` → `APP_DATA.brand/contacts` + `document.title` + `__ME_LOGO`;
  `accent_color/secondary_color` → override акцентов тем (объекты тем
  мутируются до маунта — проверено, это безопасная точка);
  `available_feature_keys` → `window.__SAAS_FEATURES` → скрытие плиток
  «Магазин»/«Баллы»/«Сертификат»/чата MAYA, если фичи нет в тарифе.
- **P3 — phone-first онбординг**: убрать тихий random-email
  (`ensureLocalSession`), вместо него UI-шаги: телефон →
  `POST /auth/phone/start` (в debug-режиме показываю `debug_code` подсказкой)
  → код → `/auth/phone/verify` → JWT в `localStorage` →
  `GET /me` на рестор сессии; если `profile_completed === false` → шаг
  «Как вас зовут?» → `PATCH /me {name}`. Коды ошибок
  `code_invalid/code_expired/too_many_attempts` → человеческие тексты.
- **P4 — идентичность в букинге**: на шаге подтверждения имя/телефон из
  `/me` (не заставляем перепечатывать; телефон read-only — `PATCH /me`
  телефон не меняет, уважаю §7); в `preview` НЕ отправляю
  `clientName/clientPhone`, если профиль полон. Маршрутизация ошибок:
  `slot_taken` → шаг слотов + auto-reload; `client_name_required` → шаг
  имени; `staff_unavailable` → шаг мастеров + reload;
  `service_not_found` → шаг услуг + reload; `validation` → подсветка.
- **P5 — экран успеха превью**: показать `total_price`, `duration_minutes`,
  `currency`, время в `branch_timezone`; язык экрана остаётся честным
  («запись не создавалась»).
- **P6 — кабинет (мини)**: экран «Мои записи (тест)» из done-экрана и при
  восстановленной сессии: `GET /appointments/my`, группировка по `timeline`
  (upcoming/past), карточка: мастер (аватар/рейтинг, деградация до `id` без
  краша — учтён tolerance note), услуги, сумма/длительность, филиал.
- **P7 — интеграционный прогон**: полный флоу против локального бэка
  (onboarding → букинг → превью → кабинет), чек-лист состояний.

## 2. Оставшиеся гэпы бэкенда — блокирующих НЕТ

Non-blocking, в формате §8:

1. Screen: выбор даты · Endpoint: **нет агрегата свободных дней** ·
   Missing: фронт делает 30 запросов `/available-slots` (по дню на месяц) ·
   Blocking: нет (работает, но шумно) · Desired delta:
   `GET /available-days?staffId&serviceIds&from&to` → `{days: ["2026-07-05", ...]}`.
2. Screen: кабинет · Endpoint: `POST /appointments/:id/cancel` (и позже
   reschedule) · Missing: действий над записью нет · Blocking: нет для
   read-only кабинета этого прохода; станет blocking для полного кабинета ·
   Desired delta: cancel с машинными кодами (`too_late_to_cancel`,
   `not_found`, `already_cancelled`).
3. Screen: онбординг · Endpoint: `/auth/phone/start` · Missing: resend-cooldown
   в ответе (`retry_after_seconds`) для честного таймера «Отправить ещё раз» ·
   Blocking: нет (сделаю таймер константой 60с).

## 3. Contract requests — нет новых

Работаю строго по `CODEX_BACKEND_HANDOFF_FOR_CLAUDE.md`. Замечание не по
контракту, а по консистентности: verify-ответ отдаёт юзера в snake_case
(`profile_completed`), `GET /me` — тоже; ок, фиксирую адаптер на snake_case.

## 4. Что не делаю (уважение §7)

Второй auth-путь, SMS-доставку, смену телефона через `PATCH /me`,
второй public-config контракт, live-booking UX без явного зелёного света.
