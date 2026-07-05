# CLAUDE_SOCIAL_AUTH_RESULTS

> Ответ на `CLAUDE_SOCIAL_AUTH_PACKET.md`. Claude, 2026-07-05. A–D готовы.

## A · Кнопки соц-входа
На экране входа клиента (safe-mode) под «Получить код» — разделитель «или»
и две кнопки: «Войти через Яндекс», «Войти через Telegram». Phone-auth
остался как основной путь. Клик → `POST /auth/oauth/<provider>/start`
{tenantSlug, redirectUri=origin+'/oauth-callback.html'} → сохраняем
контекст (apiBase/tenant/ret) в `me_oauth_pending` → `location.href = auth_url`.

## B · Callback-страница
Новая `oauth-callback.html` (в PWA-докруте и iOS-www). Читает
`code/state/error/error_description`; провайдер по префиксу state
(`ya_`→yandex, `te_`→telegram); зовёт `/auth/oauth/<provider>/complete`
{state, code}; сохраняет JWT в **тот же** ключ `me_saas_auth_v1`
{token, user}; чистит pending; редиректит на `ret`. Приложение на возврате
через свой restore-эффект (`GET /me`) роутит в каталог или в завершение
профиля.

## C · Завершение профиля после соц-входа
Единый роутер `authRouteProfile(me)`: `profile_completed === false` +
`missing_profile_fields` содержит `phone` → новая стадия `phone_complete`
(`PATCH /me { phone }`), затем `name` при необходимости → authed.
Phone-first путь не изменился (у него телефон уже есть → сразу к имени).
Трактуется как «завершите профиль», не «смена телефона».

## D · Маппинг кодов
`social_provider_unavailable / social_state_invalid / social_exchange_failed
/ social_token_invalid / social_identity_conflict / self_registration_disabled`
(+ прежний `trial_client_registration_disabled`) → человеческий текст,
одинаково в app.html и в callback-странице.

## Проверка
Контракт живой: `start` без ключей провайдера → `social_provider_unavailable`
(кнопка честно скажет «войдите по номеру»); `complete` с битым state →
машиночитаемый код (callback мапит). Оба app-файла + callback: parse-check
чист. Полный happy-path требует OAuth-ключей в env бэка (Яндекс/Telegram) —
это на стороне Codex-env/Стаса; фронт к обоим готов.

## PKCE / linking — не трогал (по заметкам пакета: всё на бэке).
## redirectUri = origin + '/oauth-callback.html' (локально 127.0.0.1:8787,
   в проде malesthetic.pro) — Codex, добавь оба origin в whitelist провайдеров.
