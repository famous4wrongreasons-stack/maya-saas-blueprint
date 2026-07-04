# CLAUDE_FINAL_PACKET_RESULTS

> Ответ на `CLAUDE_FINAL_FRONTEND_PACKET.md`. Claude, 2026-07-04. Всё выполнено.

## A · Self-serve онбординг с витрины — готово

- Новая публичная страница **`maya-start.html`**: название → авто-slug,
  e-mail/имя/телефон/пароль (опц.) → `POST /onboarding/trial` → экран
  «Салон создан» с кредами (temporary_password показывается один раз,
  null при заданном пароле) + ссылка на предпросмотр приложения.
- Кнопка «Открыть админку» передаёт JWT через `localStorage
  maya_admin_handoff` → **`maya-admin.html` авто-входит** владельцем без
  ре-логина (handoff одноразовый, ключ удаляется).
- Все 5 CTA витрины `maya-site.html` переведены на `/maya-start.html`.
- CRM на этом шаге не спрашиваем (mock ставит бэк) — по §5.

## B · booking_mode переключатель — готово

- Boot сохраняет `tenant_status / booking_mode / booking_live_enabled /
  client_registration_enabled` в `__SAAS_TENANT`.
- Сабмит: `booking_mode === 'live'` → `POST /appointments`, иначе прежний
  preview. Никакого фронтового хардкода — только конфиг (§5 соблюдён).
- Done-экран: у live свой честный текст «Запись создана в салоне — ждём
  вас!» (+цена, если бэк вернёт); preview-копия осталась честной.

## C · Поля конфига в UI — готово

- `client_registration_enabled === false` → на экране телефона заранее
  показывается «Салон готовится к запуску — вход пока только для
  сотрудников» (плюс прежний маппинг кода при попытке).

## Проверено на живом бэке

`onboarding/trial` (свежий slug): салон создан, JWT tenant_admin выдан
сразу, temporary_password есть, `status=trial`, `booking_mode=preview`;
владелец по этому JWT видит свой салон в `/admin/tenants/:id`; публичный
конфиг нового салона отдаёт `client_registration_enabled=false` (trial).
Все три страницы + оба app-файла: parse-check чист.

## Осталось на бэке (без изменений)

SMS-транспорт, включение `booking_mode=live` per-salon (после активации?),
биллинг подписок, деплой инфраструктуры.
