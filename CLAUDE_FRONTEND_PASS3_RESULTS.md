# CLAUDE_FRONTEND_PASS3_RESULTS

> Итоги фронтенд-прохода 3 (Claude). Дата: 2026-07-04.
> Файлы: `сайт и приложение/app.html` + зеркало
> `/Users/stanislavmosin/Desktop/maya-ios/www/index.html`; всё за флагом
> `?booking_backend=saas-local`; прод не тронут; деплоя не было.

## 1. Проверка фиксов Codex на живом бэке — всё подтверждено

- `POST /appointments/preview` c битым `serviceIds` → **HTTP 400
  `service_not_found`** ✓ (мой P4-роутинг теперь ведёт на шаг услуг
  с перезагрузкой, как задумано).
- `GET /services` → **реальные категории**: «Мужские стрижки» ×9,
  «Уход за бородой» ×4, «Уход» ×6 ✓.
- `available_feature_keys` МЭ → **13 ключей, включая `ai_chatbot`** ✓ —
  соответствует договорённости «МЭ = max + addon ai_chatbot» (SYNC §3).

## 2. Фронт-изменения этого прохода

1. **Категории услуг в saas-режиме**: `normalizeLocalServices` больше не
   хардкодит «Услуги» — экран выбора услуг группируется по реальным
   категориям YClients (та же группировка, что в легаси-флоу).
2. **Честный resend-таймер** на экране кода: после отправки кнопка
   «Отправить ещё раз · N с» гаснет на 60 секунд (тикающий отсчёт);
   когда бэк начнёт отдавать `retry_after_seconds` — возьмётся его
   значение автоматически (поле уже читается).
3. **Гейтинг фич включился сам** после ресинка данных: у МЭ полный
   список ключей → на главной ничего не скрыто (в т.ч. чат MAYA — ключ
   `ai_chatbot` на месте); у `demo-salon` ключа `ai_chatbot` нет → плитка
   чата скрывается. Кода не потребовалось — сработала логика P2
   (пустой список = не гейтим; непустой = гейтим).

Верификация: все правки якорные (assert count==1), `node --check` всех
inline-скриптов обоих файлов — чисто; happy-path на живом бэке зелёный
(auth → категории → слоты → превью 1800 RUB · 60 мин → мои записи).

## 3. Обратная связь бэку (формат §6)

Новых блокеров нет. Актуальный список остаётся прежним (всё non-blocking):

1. Screen: экран кода · Endpoint: `POST /auth/phone/start` ·
   Current: нет `retry_after_seconds` · Desired: добавить (фронт уже
   готов его читать, сейчас фолбэк 60с) · Non-blocking.
2. Screen: выбор даты · Endpoint: нет `GET /available-days` ·
   Current: фронт зондирует до 30 дней по-слотово · Desired: агрегат
   `{days:[...]}` · Non-blocking, **но хочу в следующем проходе**: на
   реальном YClients зондирование заметно медленнее mock'а.
3. Screen: кабинет · Endpoint: нет `POST /appointments/:id/cancel` ·
   Current: кабинет read-only · Desired: cancel с кодами
   `not_found/already_cancelled/too_late_to_cancel` · Non-blocking сейчас;
   станет первым делом кабинета, как появится.

## 4. Замечание по данным (не код)

`GET /appointments/my` до сих пор пуст у тестовых юзеров — превью записей
не создаёт (правильно). Для полировки кабинета на реальных данных нужен
либо cancel+create в safe-режиме, либо сид пары тестовых записей в
`Appointment` для tenant `muzhskaya-estetika` — на выбор Codex.

## 5. Как посмотреть глазами

Оба сервера подняты локально:
- `http://127.0.0.1:8787/app.html?booking_backend=saas-local&booking_api_base=http://localhost:3000/api&booking_tenant=muzhskaya-estetika`
  — МЭ: полный флоу, услуги теперь по категориям, resend с отсчётом.
- то же с `booking_tenant=demo-salon` — смена бренда + скрытая плитка чата
  (нет `ai_chatbot` в тарифе).
