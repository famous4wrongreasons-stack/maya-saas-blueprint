// SaaS Шаг 8 — пример: как precompiled-React index.html применяет бренд салона
// на старте. Вставляется в <head>/перед загрузкой React. Бэкенд резолвит салон
// по Host (поддомену) и отдаёт ТОЛЬКО публичный бренд (без секретов).
//
// На катовере: эндпоинт /api/tenant-config реализуется в webhook_server.py через
// tenant_config.public_config(request.host); фронт берётся из текущего index.html.

(async function applyTenantBrand() {
  let cfg;
  try {
    const r = await fetch('/api/tenant-config', { credentials: 'same-origin' });
    cfg = await r.json();              // { slug, brand:{name,logo_url,accent_color,...}, active }
  } catch (e) {
    cfg = null;                        // нет конфига → дефолтный бренд платформы
  }
  if (!cfg) return;

  const b = cfg.brand || {};

  // 1) Название (title + где надо в UI через глобал)
  if (b.name) document.title = b.name;
  window.__TENANT_BRAND = b;          // React читает отсюда вместо хардкода «Мужская Эстетика»

  // 2) Акцентный цвет → CSS-переменная (тема PWA подхватит)
  if (b.accent_color) {
    document.documentElement.style.setProperty('--accent', b.accent_color);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', b.accent_color);
  }

  // 3) Лого → favicon + переменная для шапки
  if (b.logo_url) {
    window.__TENANT_LOGO = b.logo_url;
    let link = document.querySelector('link[rel="icon"]');
    if (!link) { link = document.createElement('link'); link.rel = 'icon'; document.head.appendChild(link); }
    link.href = b.logo_url;
  }

  // 4) Подписка приостановлена → баннер «временно недоступно» (но страница оплаты жива)
  if (cfg.active === false) window.__TENANT_SUSPENDED = true;
})();

// В React-компонентах: const brand = window.__TENANT_BRAND || {};
//   <h1>{brand.name || 'Мужская Эстетика'}</h1>
//   <img src={window.__TENANT_LOGO || DEFAULT_LOGO} />
//   стили используют var(--accent) вместо захардкоженного кремового.
