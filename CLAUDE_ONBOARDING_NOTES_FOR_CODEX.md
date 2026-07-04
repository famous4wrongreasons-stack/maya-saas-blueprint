# CLAUDE_ONBOARDING_NOTES_FOR_CODEX

> Онбординг-визард реализован в админке (`сайт и приложение/maya-admin.html`,
> раздел «＋ Новый салон», виден platform_owner). Claude, 2026-07-04.
> Последовательность: POST /admin/tenants → PATCH branding (бренд+тексты)
> → PATCH/POST crm → test-crm → готовая ссылка приложения. Идемпотентный
> ретрай (упало на шаге N — тенант не дублируется). Смоук: салон «pizhon»
> создан целиком, public config отдаёт его бренд/контент.

## Гэпы бэка, найденные при сборке (формат §6)

1. Screen: визард шаг 4 · Endpoint: `POST /admin/tenants/:id/crm` ·
   Current: `apiToken` обязателен даже для `provider=mock` (validation 400) ·
   Desired: `apiToken` опционален для mock · Non-blocking — фронт шлёт
   dummy `"mock"`, но это костыль.
2. Screen: визард (весь) · Endpoint: `POST /admin/tenants` ·
   Current: создаёт tenant + пустой branding, **филиал не создаётся** —
   у нового салона нет branch → таймзона/адрес букинга берутся из
   фолбэков · Desired: авто-создание дефолтного филиала при создании
   тенанта (name/timezone из dto) ИЛИ `POST /admin/tenants/:id/branches` ·
   **Станет blocking для живой записи нового салона.**
3. Screen: финал визарда · Endpoint: нет ·
   Current: невозможно выдать салону админ-доступ (нет создания
   tenant_admin-пользователя; /auth/register создаёт client) ·
   Desired: `POST /admin/tenants/:id/users` {email, password?, role:
   tenant_admin} или инвайт-ссылка · **Blocking для передачи админки
   реальному салону.**
4. Screen: визард шаг 1 · Endpoint: нет `GET /admin/plans` ·
   Current: тариф при создании задать нельзя (planId есть в dto, а списка
   планов нет) — новый салон живёт без фич · Desired: список планов +
   сид планов из `pg/plan_catalog.py` (§6.5) · Non-blocking для demo,
   blocking для продажи.

## Что уже можно показать владельцу

Логин `owner@maya.local` → «＋ Новый салон» → 4 шага → «Открыть в админке»
→ превью справа показывает готовое брендированное приложение салона.
