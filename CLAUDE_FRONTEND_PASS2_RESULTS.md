# CLAUDE_FRONTEND_PASS2_RESULTS

> Итоги фронтенд-прохода P1–P7 (Claude). Дата: 2026-07-03.
> Все правки: `сайт и приложение/app.html` + зеркало
> `/Users/stanislavmosin/Desktop/maya-ios/www/index.html`, всё за флагом
> `?booking_backend=saas-local`. Прод не тронут, деплоя не было.

## Сделано (P1–P7 из CLAUDE_FRONTEND_PASS2_PLAN.md)

- **P1** `localBookingFetch` пробрасывает `e.code/e.field/e.body` из
  `{error:{code,...}}` (раньше объект превращался в `[object Object]`).
- **P2** White-label boot до маунта React: `GET /mobile/config/:slug` →
  бренд в `APP_DATA`/title/логотип + **акценты всех тем** (врезка в
  `tokens()`, включая хардкоды atelier-light) + кэш. Гейтинг плиток/виджетов
  по `available_feature_keys` (карта id→фича по канону `plan_catalog.py`).
  Пустой список фич трактуется как «сид не настроен» → не гейтим.
  Negative-тест: без флага — ноль побочных эффектов.
- **P3** Phone-first онбординг: экраны телефон → код (чип `debug-код`,
  «Изменить номер», «Отправить ещё раз») → имя (`PATCH /me`); сессия в
  localStorage, рестор через `GET /me`; человеческие тексты для
  `code_invalid/code_expired/too_many_attempts/delivery_unavailable`.
  Тихая random-email регистрация удалена. Каталог грузится только после входа.
- **P4** Имя/телефон префиллятся из `/me`; при полном профиле поля в
  `preview` не отправляются. Маршрутизация: `slot_taken`/`staff_unavailable`
  → шаг слотов + reload; `service_not_found` → шаг услуг + reload;
  `client_name_required` → экран имени; `validation` → указание поля.
- **P5** Экран успеха превью: `Итого: <total_price> ₽ · <duration> мин`.
- **P6** Мини-кабинет «Мои записи»: `GET /appointments/my`, группировка
  Предстоящие/Прошедшие (`timeline`), карточка мастер/услуги/филиал/сумма,
  бейдж «Отменена», деградация без краха (staff только с id и т.п.),
  retry. Входы: done-экран (кнопка вместо прод-«В кабинет») и ссылка на
  шаге мастеров.
- **P7** Живой прогон против `maya-saas-backend` на :3000 — **зелёный**
  end-to-end: конфиг (top-level форма) → start/verify (debug) → `/me` →
  `PATCH /me` → staff(5)/services(19) → слоты → превью без
  clientName/Phone (имя взято из профиля, 1800 RUB · 60 мин · Europe/Moscow)
  → неверный код → `code_invalid` → `/appointments/my`.

## Операционные заметки прогона (важно Codex)

1. Процесс на :3000 работал со вчерашней сборки — перезапущен из свежего
   `dist` (лог: `/tmp/maya-saas-backend.log`).
2. Миграции `20260703121500_phone_auth` и `20260703143000_user_profile_name`
   не были применены к локальной PG (ловилось как 500
   `column User.encryptedName does not exist`) — применил
   `npx prisma migrate deploy`. Всё дальнейшее на этой базе.

## Гэпы бэкенда (формат §8, все non-blocking)

1. Screen: подтверждение записи · Endpoint: `POST /appointments/preview` ·
   Behavior: несуществующий `serviceIds` → **HTTP 500** без машинного кода ·
   Desired: `{error:{code:"service_not_found"}}` (фронт уже умеет его
   маршрутизировать; сейчас падает в generic-ветку).
2. Screen: выбор услуги · Endpoint: `GET /services` · Field:
   `category` = `null` у всех услуг МЭ-импорта (в handoff §4.4 заявлена) ·
   Desired: прокинуть категорию YClients при импорте/чтении.
3. Screen: главная (гейтинг) · Data: `available_feature_keys` = `[]` у
   тенанта `muzhskaya-estetika` · Desired: сид `SubscriptionPlan.featuresJson`
   из `pg/plan_catalog.py` (договорённость §6.5 CODEX_RESPONSE); фронт пока
   не гейтит при пустом списке.
4. (из прошлого плана, остаётся) `retry_after_seconds` в `phone/start` для
   честного таймера resend; агрегат свободных дней; `cancel` для кабинета.

## Как посмотреть руками

1. Бэкенд уже на `http://localhost:3000` (свежий).
2. Открыть `app.html` с `?booking_backend=saas-local` → флоу: телефон →
   debug-код с экрана → имя → мастера/услуги/слоты → «Проверить локально» →
   экран превью с суммой → «Мои записи».
