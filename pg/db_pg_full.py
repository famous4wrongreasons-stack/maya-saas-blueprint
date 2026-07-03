# АВТО-СГЕНЕРИРОВАНО port_database.py из боевого database.py.
# Не править руками — менять database.py/порт и пересобирать.
# Слой Postgres+tenant; init_db не используется (схема в 01_schema_postgres.sql).

"""
Локальная база данных бота (SQLite).

Здесь хранятся персональные данные клиентов, согласия на обработку ПД
и записи. Все ПД остаются на стороне backend и НЕ передаются в AI-модель.

── Переезд на PostgreSQL (Yandex Cloud Managed PostgreSQL) ──
Схема и запросы написаны максимально переносимо. При переезде нужно:
  1. Поставить psycopg2, заменить sqlite3.connect(...) на psycopg2.connect(DSN)
  2. Плейсхолдеры '?' заменить на '%s'
  3. INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
  4. Время хранится как TEXT (ISO) — совместимо, но лучше тип TIMESTAMP
Бизнес-логика функций при этом не меняется.
"""
import os
import re
import psycopg2
import psycopg2.extras
import contextvars
from contextlib import contextmanager
from datetime import datetime, date, timedelta

import pii_crypto

DB_PATH = os.path.join(os.path.dirname(__file__), "barbershop.db")

# Версия текста согласия. Меняй при изменении Политики конфиденциальности —
# тогда у клиентов будет запрошено согласие заново.
CONSENT_VERSION = "v1.0"


# ─── Postgres + привязка к салону (SaaS-слой; заменяет sqlite3) ─────────────
PG = dict(host="/Users/stanislavmosin/saas_pg_rehearsal/pgdata", port=54329,
          user="salon_app", dbname="saas_test")

_tenant = contextvars.ContextVar("tenant_id", default=None)


def set_tenant(tenant_id):
    """Текущий салон для последующих обращений к БД (в этом контексте/задаче)."""
    _tenant.set(int(tenant_id))


def current_tenant():
    return _tenant.get()


from contextlib import contextmanager as _contextmanager


@_contextmanager
def tenant_scope(tenant_id):
    """🔴 АУДИТ-ФИКС: ставит салон на время блока и ГАРАНТИРОВАННО сбрасывает —
    чтобы stale-tenant не утёк в следующий запрос на том же потоке/таске.
    Каждая точка входа (telegram-апдейт, HTTP-запрос, джоба) должна оборачивать
    работу в `with tenant_scope(tid):`."""
    token = _tenant.set(int(tenant_id))
    try:
        yield
    finally:
        _tenant.reset(token)


class _Cur:
    """Курсор как у sqlite: fetchone/fetchall/rowcount + lastrowid через lastval()."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        self._cur.execute("SELECT lastval() AS id")
        return self._cur.fetchone()["id"]


class _Conn:
    """conn.execute(sql, params) как у sqlite: '?'→'%s', экранирует литеральные
    '%', INSERT OR IGNORE → ON CONFLICT DO NOTHING, строки dict-ом (row['col'])."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        if re.search(r"(?i)insert\s+or\s+ignore\s+into", sql):
            sql = re.sub(r"(?i)insert\s+or\s+ignore\s+into", "INSERT INTO", sql)
            sql += " ON CONFLICT DO NOTHING"
        sql = sql.replace("%", "%%").replace("?", "%s")
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return _Cur(cur)

    def executescript(self, _sql):
        raise NotImplementedError("Схема Postgres — в 01_schema_postgres.sql, init_db не нужен")


@contextmanager
def _db():
    """Соединение под ТЕКУЩИЙ салон: SET app.tenant_id, commit при успехе."""
    tid = _tenant.get()
    if tid is None:
        raise RuntimeError("tenant не задан: вызови set_tenant(<id>) до работы с БД")
    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as c:
            # SET LOCAL — действует только в этой транзакции (безопасно для пула
            # соединений: не протекает на следующего арендатора соединения).
            c.execute("SET LOCAL app.tenant_id = %s", (str(tid),))
        yield _Conn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывать один раз при старте бота."""
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_chat_id  INTEGER UNIQUE,
                name              TEXT,
                phone             TEXT,
                created_at        TEXT,
                updated_at        TEXT
            );
            CREATE TABLE IF NOT EXISTS consents (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id        INTEGER NOT NULL,
                consent_given    INTEGER NOT NULL,
                consent_at       TEXT    NOT NULL,
                consent_version  TEXT    NOT NULL,
                source           TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id           INTEGER NOT NULL,
                service             TEXT,
                master              TEXT,
                datetime            TEXT,
                yclients_record_id  INTEGER,
                created_at          TEXT,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE TABLE IF NOT EXISTS gift_certificates (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                code                TEXT    UNIQUE NOT NULL,
                amount              INTEGER NOT NULL,
                recipient_phone     TEXT    NOT NULL,
                recipient_name      TEXT,
                buyer_chat_id       INTEGER,
                issued_at           TEXT    NOT NULL,
                expires_at          TEXT    NOT NULL,
                used_at             TEXT,
                used_by_admin_id    INTEGER,
                payment_status      TEXT    NOT NULL DEFAULT 'pending',
                yukassa_payment_id  TEXT
            );
            CREATE TABLE IF NOT EXISTS admins (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id  INTEGER UNIQUE NOT NULL,
                added_at          TEXT    NOT NULL,
                added_by_user_id  INTEGER
            );

            -- ─── Уведомления мастерам о новых записях ────────────────────

            -- Связка между мастером в YClients и его Telegram-аккаунтом.
            -- Мастер привязывается через /bind <код> в нашем боте, после чего
            -- получает уведомления о своих новых записях с AI-советом по апсейлу.
            CREATE TABLE IF NOT EXISTS masters_telegram (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                yclients_staff_id  INTEGER UNIQUE NOT NULL,
                telegram_chat_id   INTEGER UNIQUE,
                full_name          TEXT,
                bind_code          TEXT    UNIQUE,
                bound_at           TEXT,
                is_active          INTEGER NOT NULL DEFAULT 1,
                mute_until         TEXT,    -- ISO datetime, до этого момента уведомления глушим
                created_at         TEXT    NOT NULL
            );

            -- Дедупликация YClients-webhook'ов. YClients может прислать одно
            -- событие несколько раз — обрабатываем record_id только однократно.
            CREATE TABLE IF NOT EXISTS processed_records (
                record_id     INTEGER PRIMARY KEY,
                event_type    TEXT    NOT NULL,
                processed_at  TEXT    NOT NULL
            );

            -- Снимок состояния записи YClients. Нужен, чтобы на webhook update
            -- понимать, ЧТО именно изменилось (мастер / время / услуги), и не
            -- слать мастеру «запись изменена» на каждое касание (закрытие
            -- оплаты, финопер). Обновляется на create и update.
            CREATE TABLE IF NOT EXISTS record_state (
                record_id     INTEGER PRIMARY KEY,
                staff_id      INTEGER,
                datetime      TEXT,
                services_sig  TEXT,
                attendance    INTEGER,
                updated_at    TEXT    NOT NULL
            );

            -- Кеш истории визитов клиента (24ч). Нужен, чтобы не дёргать
            -- YClients API на каждое уведомление (rate limit ~200/мин).
            CREATE TABLE IF NOT EXISTS client_history_cache (
                client_id     INTEGER PRIMARY KEY,
                history_json  TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            );

            -- Лог AI-советов и реакций мастера для последующей аналитики:
            -- помогает ли AI поднимать средний чек.
            CREATE TABLE IF NOT EXISTS ai_advice_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id           INTEGER NOT NULL,
                client_id           INTEGER,
                staff_id            INTEGER NOT NULL,
                ai_provider         TEXT,         -- 'claude' | 'openai' | 'fallback'
                advice_text         TEXT,
                button_pressed      TEXT,         -- 'cash' | 'card' | NULL
                payment_method      TEXT,
                final_check_amount  INTEGER,      -- ₽
                created_at          TEXT    NOT NULL,
                closed_at           TEXT
            );

            -- Простой key-value стор для рантайм-настроек, которые меняются
            -- админскими командами (например, текущий AI-провайдер для
            -- уведомлений мастерам). Перебивает значения из config.py.
            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  TEXT NOT NULL
            );

            -- Минимальный реестр салонов-подписчиков MAYA (founder/GOD-режим).
            -- Пока мульти-салонная SaaS не запущена — владелец ведёт его вручную;
            -- когда появится авто-онбординг, он будет писать сюда же.
            CREATE TABLE IF NOT EXISTS maya_tenants (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                city         TEXT,
                plan         TEXT,
                status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|active|suspended
                owner_name   TEXT,
                phone        TEXT,
                mrr          INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                activated_at TEXT
            );

            -- Чаевые: каждый факт «Я перевёл» от клиента (служебный сигнал, не
            -- банковское подтверждение). Для аналитики по каждому мастеру отдельно.
            CREATE TABLE IF NOT EXISTS tips (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id    INTEGER,
                master_slug  TEXT,
                master_name  TEXT,
                amount       INTEGER NOT NULL DEFAULT 0,
                record_id    INTEGER,
                created_at   TEXT    NOT NULL
            );

            -- Лог реактивационных сообщений уснувшим клиентам.
            -- Защита от спама: не шлём чаще 1 раза в 14 дней.
            -- action: 'sent' | 'declined' | 'engaged' | 'blocked'
            CREATE TABLE IF NOT EXISTS reactivation_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL,
                action      TEXT    NOT NULL,
                sent_at     TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );

            -- Лог напоминаний по индивидуальному циклу.
            -- Защита от спама: не шлём одному клиенту чаще 1 раза в 10 дней.
            CREATE TABLE IF NOT EXISTS cycle_reminder_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL,
                avg_cycle_days     INTEGER NOT NULL,
                predicted_visit    TEXT    NOT NULL,
                action             TEXT    NOT NULL DEFAULT 'sent',  -- sent|engaged|declined|blocked
                sent_at            TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );

            -- Промокоды на день рождения. Один промокод на ДР каждого года
            -- (защита от дублирования внутри года + от случайного двойного запуска).
            CREATE TABLE IF NOT EXISTS birthday_promo (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL,
                code        TEXT    UNIQUE NOT NULL,
                percent     INTEGER NOT NULL DEFAULT 20,
                year        INTEGER NOT NULL,
                sent_at     TEXT    NOT NULL,
                expires_at  TEXT    NOT NULL,
                used_at     TEXT,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_birthday_promo_client_year
                ON birthday_promo (client_id, year);

            -- Программа лояльности: транзакции изменения баланса баллов
            -- (начисление за визит, погашение по коду, сгорание).
            -- Баланс = SUM(points) — без отдельной таблицы балансов.
            CREATE TABLE IF NOT EXISTS loyalty_transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL,
                type            TEXT    NOT NULL,
                points          INTEGER NOT NULL,
                visit_record_id INTEGER,
                note            TEXT,
                at              TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_loyalty_tx_client
                ON loyalty_transactions (client_id);
            -- Уникальность начисления за один визит (защита от двойного earn)
            CREATE UNIQUE INDEX IF NOT EXISTS idx_loyalty_tx_earn_unique
                ON loyalty_transactions (client_id, visit_record_id, type)
                WHERE type = 'earn' AND visit_record_id IS NOT NULL;

            -- Одноразовые коды для списания баллов на услугу-уход.
            CREATE TABLE IF NOT EXISTS loyalty_redeem_codes (
                code            TEXT    PRIMARY KEY,
                client_id       INTEGER NOT NULL,
                service_title   TEXT    NOT NULL,
                points          INTEGER NOT NULL,
                issued_at       TEXT    NOT NULL,
                expires_at      TEXT    NOT NULL,
                used_at          TEXT,
                used_by_admin_id INTEGER,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_loyalty_codes_client
                ON loyalty_redeem_codes (client_id);

            -- Абонементы: купленные клиентами подписки на месячный пакет
            -- визитов. plan_code — ключ в каталоге subscriptions.PLANS.
            -- Статусы: pending_payment / active / expired / refunded.
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id               INTEGER NOT NULL,
                plan_code               TEXT    NOT NULL,
                tier                    TEXT    NOT NULL DEFAULT 'top',
                yukassa_payment_id      TEXT,
                price_rub               INTEGER NOT NULL,
                visits_included         INTEGER NOT NULL,
                visits_used             INTEGER NOT NULL DEFAULT 0,
                started_at              TEXT    NOT NULL,
                expires_at              TEXT    NOT NULL,
                status                  TEXT    NOT NULL DEFAULT 'pending_payment',
                payment_completed_at    TEXT,
                renew_reminder_sent_at  TEXT,
                created_at              TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_subscriptions_client_status
                ON subscriptions (client_id, status);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_status_expires
                ON subscriptions (status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_payment
                ON subscriptions (yukassa_payment_id);

            -- Реферальная программа: код у каждого клиента, привязка
            -- «приглашённый → пригласивший», выданные промокоды.
            CREATE TABLE IF NOT EXISTS referral_codes (
                client_id   INTEGER PRIMARY KEY,
                code        TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_referral_codes_code
                ON referral_codes (code);

            CREATE TABLE IF NOT EXISTS referrals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_client_id  INTEGER NOT NULL,
                referee_chat_id     INTEGER NOT NULL,
                referee_client_id   INTEGER,
                code_used           TEXT    NOT NULL,
                joined_at           TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'pending',
                FOREIGN KEY (referrer_client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_referrals_referee_chat
                ON referrals (referee_chat_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                ON referrals (referrer_client_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_status
                ON referrals (status);

            CREATE TABLE IF NOT EXISTS referral_promos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id INTEGER NOT NULL,
                client_id   INTEGER NOT NULL,
                code        TEXT    NOT NULL UNIQUE,
                kind        TEXT    NOT NULL,
                percent     INTEGER NOT NULL DEFAULT 15,
                issued_at   TEXT    NOT NULL,
                expires_at  TEXT    NOT NULL,
                used_at     TEXT,
                FOREIGN KEY (referral_id) REFERENCES referrals(id),
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );

            -- Журнал предложений освободившегося слота. Каждое сообщение
            -- «у Стаса освободилось 18:00 завтра, будешь?» сюда пишется.
            -- Используется для антиспама (не чаще раза в 7 дней одному клиенту).
            CREATE TABLE IF NOT EXISTS freed_slot_offers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL,
                staff_id        INTEGER NOT NULL,
                slot_datetime   TEXT    NOT NULL,
                offered_at      TEXT    NOT NULL,
                action          TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_freed_offers_client
                ON freed_slot_offers (client_id, offered_at);
            CREATE INDEX IF NOT EXISTS idx_freed_offers_slot
                ON freed_slot_offers (slot_datetime);

            -- Журнал расхода токенов на ИИ — для команды /ai_cost и контроля
            -- месячного бюджета на AI-стилиста.
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                at                  TEXT    NOT NULL,
                feature             TEXT    NOT NULL,
                model               TEXT    NOT NULL,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
                cost_usd            REAL    NOT NULL DEFAULT 0,
                user_id             INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ai_usage_at ON ai_usage_log (at);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_feature ON ai_usage_log (feature);

            -- Журнал согласий клиента на отправку фото в AI-стилист.
            -- Это отдельное согласие (трансграничная передача в AI-сервис),
            -- по 152-ФЗ должно быть в письменной форме с подтверждением.
            -- Согласие фиксируется при нажатии кнопки «✅ Согласен» в боте.
            CREATE TABLE IF NOT EXISTS stylist_consents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL,
                telegram_chat_id INTEGER,
                consent_given   INTEGER NOT NULL,
                consent_at      TEXT    NOT NULL,
                consent_version TEXT    NOT NULL,
                source          TEXT    NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_stylist_consents_client
                ON stylist_consents (client_id);

            -- Сбор отзывов после визита. Один запрос на client+record_id;
            -- статус «отправлен / ответил / отказался / просрочен».
            CREATE TABLE IF NOT EXISTS review_requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL,
                record_id       INTEGER NOT NULL,
                staff_id        INTEGER,
                visit_closed_at TEXT    NOT NULL,
                send_after      TEXT    NOT NULL,
                sent_at         TEXT,
                responded_at    TEXT,
                rating          INTEGER,
                comment         TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending',
                FOREIGN KEY (client_id) REFERENCES clients(id),
                UNIQUE (client_id, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_review_requests_status_send
                ON review_requests (status, send_after);
            CREATE INDEX IF NOT EXISTS idx_review_requests_client
                ON review_requests (client_id);

            -- Состояние «текущего открытого диалога» клиента с ботом.
            -- Нужно для алерта о зависшей заявке: если клиент писал,
            -- Антон отвечал, а 30 мин спустя нет ни записи, ни явного отказа —
            -- пингуем владельца.
            CREATE TABLE IF NOT EXISTS client_chat_state (
                client_id              INTEGER PRIMARY KEY,
                last_client_message_at TEXT,
                last_client_message    TEXT,
                last_ai_reply          TEXT,
                last_ai_reply_at       TEXT,
                resolved_reason        TEXT,
                resolved_at            TEXT,
                alerted_at             TEXT,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );
            CREATE INDEX IF NOT EXISTS idx_client_chat_state_unresolved
                ON client_chat_state (resolved_at, alerted_at);

            -- ── Веб-вход без Telegram (VK ID / телефон) ──────────────────
            -- Код подтверждения по телефону: flash-call (код = последние
            -- цифры входящего номера) или SMS. Храним ХЕШ кода, не сам код.
            CREATE TABLE IF NOT EXISTS web_login_codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_hash  TEXT NOT NULL,
                code_hash   TEXT NOT NULL,
                channel     TEXT NOT NULL DEFAULT 'call',   -- 'call' | 'sms'
                attempts    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_web_login_codes_phone
                ON web_login_codes (phone_hash, expires_at);

            -- Сессия веб-входа: токен в браузере вместо Telegram initData.
            -- subject_kind — клиент (по телефону) или сотрудник (мастер/админ).
            -- phone_hash — основной ключ привязки к YClients; chat_id
            -- заполняется, если телефон сматчился с Telegram-клиентом.
            CREATE TABLE IF NOT EXISTS web_sessions (
                token        TEXT PRIMARY KEY,
                subject_kind TEXT NOT NULL DEFAULT 'client',  -- 'client' | 'staff'
                chat_id      INTEGER,
                phone_hash   TEXT,
                vk_user_id   INTEGER,
                display_name TEXT,
                tg_first_name TEXT,
                tg_last_name  TEXT,
                tg_username   TEXT,
                tg_photo_url  TEXT,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                last_seen_at TEXT,
                revoked      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_web_sessions_phone
                ON web_sessions (phone_hash);

            -- CutMatch: счётчик ИИ-консультаций по подбору стрижки.
            -- Лимит 2/день на пользователя (user_id = telegram chat_id / сессия).
            CREATE TABLE IF NOT EXISTS cutmatch_usage (
                user_id INTEGER NOT NULL,
                day     TEXT    NOT NULL,   -- YYYY-MM-DD
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );

            -- Расходы по салону, которые присылает ассистент Антон (кофе, уборщица,
            -- касс. лента и т.п.). Попадают в дневной отчёт владельцу за свою дату.
            CREATE TABLE IF NOT EXISTS salon_expenses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT    NOT NULL,   -- YYYY-MM-DD (день, к которому относится расход)
                item       TEXT    NOT NULL,   -- что куплено/оплачено
                amount     INTEGER NOT NULL,   -- рубли
                source     TEXT    NOT NULL DEFAULT 'anton',
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_salon_expenses_date
                ON salon_expenses (date);

            -- Касса со слов Антона: сколько всего налички в кассе и сколько получено
            -- наличкой за конкретный день. Для сверки с расчётной наличкой YClients
            -- в дневном отчёте владельца. Один день = одна запись (перезапись).
            CREATE TABLE IF NOT EXISTS cash_log (
                date        TEXT    PRIMARY KEY,   -- YYYY-MM-DD
                total_till  INTEGER NOT NULL,      -- всего налички в кассе сейчас
                day_cash    INTEGER NOT NULL,      -- наличкой получено за этот день
                entered_by  INTEGER,
                ts          TEXT    NOT NULL
            );

            -- Лист ожидания на ЗАНЯТОЕ время. Клиент в чате спросил конкретный
            -- слот, а он занят → запоминаем. Если слот освободится — пишем ему
            -- ПЕРВЫМ (приоритет над скорингом по циклу в freed_slot).
            CREATE TABLE IF NOT EXISTS slot_waitlist (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id     INTEGER NOT NULL,
                chat_id       INTEGER,
                staff_id      INTEGER NOT NULL,
                slot_datetime TEXT    NOT NULL,   -- ISO 'YYYY-MM-DDTHH:MM'
                created_at    TEXT    NOT NULL,
                notified_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_slot_waitlist_lookup
                ON slot_waitlist (staff_id, slot_datetime, notified_at);

            -- «Настроение визита» (пилюли как в Матрице): клиент при записи
            -- выбирает, как он настроен на приём — 🔴 'red' (хочет помолчать,
            -- тишина) или 🔵 'blue' (в хорошем настроении, готов общаться).
            -- Ключ — record_id YClients (выбор привязан к КОНКРЕТНОМУ визиту).
            -- Показывается барберу в журнале/пуше/комментарии записи.
            CREATE TABLE IF NOT EXISTS visit_mood (
                record_id   INTEGER PRIMARY KEY,   -- YClients record_id
                mood        TEXT    NOT NULL,      -- 'red' | 'blue'
                source      TEXT,                  -- 'bot' | 'app' | 'admin'
                client_id   INTEGER,               -- для пер-клиентской памяти (не обязателен)
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );
            -- Профиль предпочтений клиента (Фаза 2 «наставник»). ОБЕЗЛИЧЕННО:
            -- ключ — локальный clients.id, в prefs только привычки/предпочтения
            -- («любит фейд», «не любит болтать», «кофе без сахара»), БЕЗ ПД.
            -- Накапливается из диалога MAYA; показывается мастеру в досье и
            -- подмешивается в клиентский контекст. Прогоняется через redact_pii.
            CREATE TABLE IF NOT EXISTS client_preferences (
                client_id   INTEGER PRIMARY KEY,   -- clients.id (локальный)
                prefs       TEXT    NOT NULL,       -- по одному предпочтению на строку
                updated_at  TEXT    NOT NULL
            );
        """)
        # Аудит вызовов инструментов LLM (RBAC/risk-tiering): кто, что, разрешено ли.
        # Дебаг + 152-ФЗ + питает GOD-режим «Здоровье». ПД не пишем (только имя инструмента).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_audit (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT    NOT NULL,
                user_id  INTEGER,
                role     TEXT,
                tool     TEXT    NOT NULL,
                risk     TEXT,
                allowed  INTEGER NOT NULL,    -- 1 разрешено, 0 отказано гейтом
                reason   TEXT
            );
        """)
        # Durable-идемпотентность оплаты визита: один record_id = одна оплата, даже
        # после рестарта процесса или вытеснения in-memory локов (гонка двойного тапа).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_idempotency (
                record_id INTEGER PRIMARY KEY,
                method    TEXT    NOT NULL,
                amount    INTEGER,
                ts        TEXT    NOT NULL
            );
        """)
        # Procedural-память: операционные правила салона, заданные владельцем словами
        # («новым клиентам предлагай комплекс», «парковка бесплатная во дворе»). MAYA
        # подхватывает их в системный промпт и соблюдает в работе. Без ПД и секретов.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salon_rules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text  TEXT    NOT NULL,
                created_by INTEGER,
                created_at TEXT    NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1
            );
        """)
        # Миграция: добавляем зашифрованные колонки в clients и gift_certificates
        _migrate_add_encrypted_columns(conn)
        _backfill_encryption(conn)


def _migrate_add_encrypted_columns(conn):
    """Добавляет столбцы для зашифрованных ПД, если их ещё нет."""
    def _add(table: str, column: str, type_: str = "TEXT"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
        except psycopg2.OperationalError:
            pass  # столбец уже есть

    _add("clients", "name_enc")
    _add("clients", "phone_enc")
    _add("clients", "phone_hash")
    _add("gift_certificates", "recipient_name_enc")
    _add("gift_certificates", "recipient_phone_enc")
    _add("gift_certificates", "recipient_phone_hash")
    # Колонка tier на subscriptions — добавилась поздно, на боевой БД её ещё нет
    _add("subscriptions", "tier", "TEXT NOT NULL DEFAULT 'top'")
    # Роль «кассир» для мастеров — даёт право гасить баллы лояльности
    # и подарочные сертификаты без необходимости быть полным админом.
    _add("masters_telegram", "can_redeem", "INTEGER NOT NULL DEFAULT 0")
    # Флаг «в диалоге было намерение записаться» — чтобы lead-alert
    # срабатывал только на реальные заявки, а не на «привет / как дела».
    _add("client_chat_state", "booking_intent", "INTEGER NOT NULL DEFAULT 0")
    # Состав услуг до и после совета AI — для аналитики «зашёл/не зашёл»
    _add("ai_advice_log", "initial_services_json", "TEXT")
    _add("ai_advice_log", "final_services_json", "TEXT")
    # Какую версию «что нового» клиент уже видел — чтобы не показывать дважды
    _add("clients", "whats_new_seen_version", "TEXT")
    # Согласие на маркетинговые рассылки (ст.18 «О рекламе» + 152-ФЗ).
    # NULL — клиент не давал согласия. ISO-дата — момент согласия.
    # При отзыве — *_at очищаем, *_revoked_at ставим. Для возможной проверки
    # хранятся оба поля параллельно.
    _add("clients", "marketing_consent_at", "TEXT")
    _add("clients", "marketing_consent_revoked_at", "TEXT")

    # Атрибуция источника привлечения — фиксируется на ПЕРВОМ /start.
    # Формат: «direct», «ad:direct_jan2026», «qr:check», «ref:REF-XXXXXX»,
    # «site:gift_cert», «app:book», «migration», «other:<payload>».
    # Используется в /sources_stats — оценка эффективности каналов привлечения.
    _add("clients", "first_source", "TEXT")
    _add("clients", "first_source_at", "TEXT")

    # Последнее выбранное «настроение визита» клиента ('red'|'blue') — чтобы при
    # следующей записи можно было предложить тот же выбор по умолчанию.
    _add("clients", "default_visit_mood", "TEXT")
    # Профиль Telegram в web-сессии: deep-link вход в приложении должен помнить
    # имя, фамилию, username и аватар, а не только first_name.
    _add("web_sessions", "tg_first_name", "TEXT")
    _add("web_sessions", "tg_last_name", "TEXT")
    _add("web_sessions", "tg_username", "TEXT")
    _add("web_sessions", "tg_photo_url", "TEXT")


def _backfill_encryption(conn):
    """
    Один раз перекодирует существующие записи с plain-text именем/телефоном
    в зашифрованный вид. Plain-колонки после этого зануляются.
    Идемпотентно: повторные запуски не делают лишней работы.
    """
    # clients
    rows = conn.execute(
        "SELECT id, name, phone FROM clients "
        "WHERE (name IS NOT NULL AND name != '') OR (phone IS NOT NULL AND phone != '')"
    ).fetchall()
    for r in rows:
        name_enc = pii_crypto.encrypt(r["name"]) if r["name"] else None
        phone_enc = pii_crypto.encrypt(r["phone"]) if r["phone"] else None
        phone_hash = pii_crypto.hash_phone(r["phone"])
        conn.execute(
            "UPDATE clients SET name_enc = ?, phone_enc = ?, phone_hash = ?, "
            "name = NULL, phone = NULL WHERE id = ?",
            (name_enc, phone_enc, phone_hash, r["id"]),
        )

    # gift_certificates. recipient_phone имеет NOT NULL — занулять нельзя,
    # ставим пустую строку. Это технический legacy-столбец, реальные данные
    # теперь в recipient_phone_enc.
    rows = conn.execute(
        "SELECT id, recipient_name, recipient_phone FROM gift_certificates "
        "WHERE (recipient_name IS NOT NULL AND recipient_name != '') "
        "OR (recipient_phone IS NOT NULL AND recipient_phone != '')"
    ).fetchall()
    for r in rows:
        name_enc = pii_crypto.encrypt(r["recipient_name"]) if r["recipient_name"] else None
        phone_enc = pii_crypto.encrypt(r["recipient_phone"]) if r["recipient_phone"] else None
        phone_hash = pii_crypto.hash_phone(r["recipient_phone"])
        conn.execute(
            "UPDATE gift_certificates SET recipient_name_enc = ?, "
            "recipient_phone_enc = ?, recipient_phone_hash = ?, "
            "recipient_name = NULL, recipient_phone = '' WHERE id = ?",
            (name_enc, phone_enc, phone_hash, r["id"]),
        )


# ─── Клиенты (персональные данные) ──────────────────────────────────────

def get_or_create_client(telegram_chat_id: int) -> int:
    """Возвращает внутренний id клиента, создаёт запись если её ещё нет."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO clients (telegram_chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (telegram_chat_id, _now(), _now()),
        )
        return cur.lastrowid


def update_client(client_id: int, name: str = None, phone: str = None):
    """
    Сохраняет/обновляет персональные данные клиента.
    На вход — plaintext. В БД пишет только зашифрованные значения +
    HMAC телефона для поиска. Plain-колонки name/phone не используются.
    """
    fields, values = [], []
    if name is not None:
        fields.append("name_enc = ?")
        values.append(pii_crypto.encrypt(name))
    if phone is not None:
        fields.append("phone_enc = ?")
        values.append(pii_crypto.encrypt(phone))
        fields.append("phone_hash = ?")
        values.append(pii_crypto.hash_phone(phone))
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(client_id)
    with _db() as conn:
        conn.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id = ?", values)


def _client_row_to_dict(row) -> dict:
    """Расшифровывает name/phone из строки clients и возвращает удобный dict."""
    d = dict(row)
    d["name"] = pii_crypto.decrypt(d.get("name_enc"))
    d["phone"] = pii_crypto.decrypt(d.get("phone_enc"))
    return d


def get_client(telegram_chat_id: int) -> dict | None:
    """Возвращает запись клиента по Telegram chat_id с расшифрованными ПД."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        return _client_row_to_dict(row) if row else None


def get_client_by_id(client_id: int) -> dict | None:
    """Возвращает запись клиента по id с расшифрованными ПД."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,),
        ).fetchone()
        return _client_row_to_dict(row) if row else None


def get_whats_new_seen(telegram_chat_id: int) -> str | None:
    """Какую версию «что нового» уже видел клиент."""
    with _db() as conn:
        row = conn.execute(
            "SELECT whats_new_seen_version FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        return row["whats_new_seen_version"] if row else None


def mark_whats_new_seen(telegram_chat_id: int, version: str):
    """Помечает что клиент увидел версию what's-new."""
    with _db() as conn:
        conn.execute(
            "UPDATE clients SET whats_new_seen_version = ? "
            "WHERE telegram_chat_id = ?",
            (version, telegram_chat_id),
        )


# ─── Атрибуция источника привлечения ────────────────────────────────

def record_first_source(client_id: int, source: str) -> bool:
    """
    Записывает источник привлечения по first-touch: если у клиента уже есть
    first_source — НЕ перезаписываем. Возвращает True если только что
    записали, False если уже было раньше.
    """
    if not source:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT first_source FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not row:
            return False
        if row["first_source"]:
            return False
        conn.execute(
            "UPDATE clients SET first_source = ?, first_source_at = ? "
            "WHERE id = ? AND first_source IS NULL",
            (source[:100], _now(), client_id),
        )
        return True


def sources_stats(days: int = 30) -> dict:
    """
    Сводка по источникам привлечения за период.
    Возвращает: total, by_source (dict с метриками per-source).
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT first_source AS src,
                   COUNT(*) AS clients,
                   SUM(CASE WHEN consent_at IS NOT NULL THEN 1 ELSE 0 END) AS with_pd_consent
              FROM clients
             WHERE first_source_at >= ?
          GROUP BY first_source
          ORDER BY clients DESC
            """,
            (cutoff,),
        ).fetchall()
        by_source = {r["src"] or "unknown": dict(r) for r in rows}
        total = sum(r["clients"] for r in rows)
        # Из тех, кого привлекли в этом окне, сколько уже сделали запись
        booked_rows = conn.execute(
            """
            SELECT c.first_source AS src, COUNT(DISTINCT c.id) AS booked
              FROM clients c
              JOIN bookings b ON b.client_id = c.id
             WHERE c.first_source_at >= ?
          GROUP BY c.first_source
            """,
            (cutoff,),
        ).fetchall()
        booked_by_source = {r["src"] or "unknown": r["booked"] for r in booked_rows}
        for src, data in by_source.items():
            data["booked"] = booked_by_source.get(src, 0)
            data["conversion"] = (
                round(data["booked"] / data["clients"] * 100, 1)
                if data["clients"] else 0.0
            )
    return {"total": total, "by_source": by_source, "days": days}


def find_client_by_phone(phone: str) -> dict | None:
    """
    Быстрый поиск клиента по телефону через HMAC-хеш (без расшифровки всех записей).
    Возвращает расшифрованный dict либо None.
    """
    phone_hash = pii_crypto.hash_phone(phone)
    if not phone_hash:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE phone_hash = ? LIMIT 1",
            (phone_hash,),
        ).fetchone()
        return _client_row_to_dict(row) if row else None


# ─── Профиль предпочтений клиента (Фаза 2 «наставник») ──────────────────
# Обезличенно: ключ — локальный clients.id, в prefs только привычки.
# ПД не хранятся (вызывающий код прогоняет текст через anonymizer.redact_pii).

_PREF_MAX_LINES = 12
_PREF_MAX_LEN = 800


def add_client_preference(telegram_chat_id: int, pref: str) -> bool:
    """Добавляет одно предпочтение клиента (по chat_id). Дедуп без учёта
    регистра, копит до _PREF_MAX_LINES строк. Возвращает True, если записано."""
    pref = (pref or "").strip().strip("•- ").strip()
    if not pref or len(pref) > 200:
        return False
    client_id = get_or_create_client(int(telegram_chat_id))
    with _db() as conn:
        row = conn.execute(
            "SELECT prefs FROM client_preferences WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        lines = [l for l in (row["prefs"].split("\n") if row else []) if l.strip()]
        if any(pref.lower() == l.lower() for l in lines):
            return False                       # уже есть
        lines.append(pref)
        lines = lines[-_PREF_MAX_LINES:]       # держим последние N
        text = "\n".join(lines)[:_PREF_MAX_LEN]
        conn.execute(
            "INSERT INTO client_preferences (client_id, prefs, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET "
            "prefs = excluded.prefs, updated_at = excluded.updated_at",
            (client_id, text, _now()),
        )
    return True


def get_client_preferences(telegram_chat_id: int) -> str:
    """Предпочтения клиента по telegram chat_id (или '' если нет)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT p.prefs FROM client_preferences p "
            "JOIN clients c ON c.id = p.client_id WHERE c.telegram_chat_id = ?",
            (int(telegram_chat_id),),
        ).fetchone()
        return (row["prefs"] if row else "") or ""


def get_client_preferences_by_phone(phone: str) -> str:
    """Предпочтения клиента по телефону (для досье мастеру). '' если нет."""
    cl = find_client_by_phone(phone)
    if not cl or not cl.get("id"):
        return ""
    with _db() as conn:
        row = conn.execute(
            "SELECT prefs FROM client_preferences WHERE client_id = ?",
            (cl["id"],),
        ).fetchone()
        return (row["prefs"] if row else "") or ""


# ─── Веб-вход без Telegram (VK ID / телефон): коды и сессии ──────────────

def save_web_login_code(phone_hash: str, code_hash: str, channel: str = "call",
                        ttl_minutes: int = 5):
    """Сохраняет ХЕШ кода подтверждения. Прежние неиспользованные коды этого
    телефона гасим — активным остаётся только последний запрос."""
    if not phone_hash or not code_hash:
        return
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")
    exp_s = (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "UPDATE web_login_codes SET consumed_at = ? "
            "WHERE phone_hash = ? AND consumed_at IS NULL",
            (now_s, phone_hash),
        )
        conn.execute(
            "INSERT INTO web_login_codes "
            "(phone_hash, code_hash, channel, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (phone_hash, code_hash, channel, now_s, exp_s),
        )


def verify_web_login_code(phone_hash: str, code_hash: str, max_attempts: int = 5) -> bool:
    """Проверяет код: не истёк, не использован, лимит попыток не превышен.
    Успех → помечает использованным. Неверный код → +1 к попыткам."""
    if not phone_hash or not code_hash:
        return False
    now_s = datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT id, code_hash, attempts FROM web_login_codes "
            "WHERE phone_hash = ? AND consumed_at IS NULL AND expires_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (phone_hash, now_s),
        ).fetchone()
        if not row or row["attempts"] >= max_attempts:
            return False
        if row["code_hash"] != code_hash:
            conn.execute(
                "UPDATE web_login_codes SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            return False
        conn.execute(
            "UPDATE web_login_codes SET consumed_at = ? WHERE id = ?",
            (now_s, row["id"]),
        )
        return True


def create_web_session(token: str, *, phone_hash: str | None = None,
                       chat_id: int | None = None, vk_user_id: int | None = None,
                       display_name: str = "", subject_kind: str = "client",
                       tg_first_name: str = "", tg_last_name: str = "",
                       tg_username: str = "", tg_photo_url: str = "",
                       ttl_days: int = 30):
    """Создаёт сессию веб-входа (токен в браузере вместо Telegram initData)."""
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")
    exp_s = (now + timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO web_sessions "
            "(token, subject_kind, chat_id, phone_hash, vk_user_id, display_name, "
            " tg_first_name, tg_last_name, tg_username, tg_photo_url, "
            " created_at, expires_at, last_seen_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT (token) DO UPDATE SET "
            "  subject_kind=excluded.subject_kind, chat_id=excluded.chat_id, "
            "  phone_hash=excluded.phone_hash, vk_user_id=excluded.vk_user_id, "
            "  display_name=excluded.display_name, "
            "  tg_first_name=excluded.tg_first_name, tg_last_name=excluded.tg_last_name, "
            "  tg_username=excluded.tg_username, tg_photo_url=excluded.tg_photo_url, "
            "  created_at=excluded.created_at, "
            "  expires_at=excluded.expires_at, last_seen_at=excluded.last_seen_at, "
            "  revoked=excluded.revoked",
            (token, subject_kind, chat_id, phone_hash, vk_user_id,
             display_name, tg_first_name, tg_last_name, tg_username, tg_photo_url,
             now_s, exp_s, now_s),
        )


def get_web_session(token: str) -> dict | None:
    """Возвращает живую (не отозванную, не истёкшую) сессию по токену + обновляет
    last_seen_at. Иначе None."""
    if not token:
        return None
    now_s = datetime.now().isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM web_sessions "
            "WHERE token = ? AND revoked = 0 AND expires_at >= ?",
            (token, now_s),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE web_sessions SET last_seen_at = ? WHERE token = ?",
            (now_s, token),
        )
        return dict(row)


def revoke_web_session(token: str):
    with _db() as conn:
        conn.execute("UPDATE web_sessions SET revoked = 1 WHERE token = ?", (token,))


# ─── Нативный вход через Telegram (deep-link + опрос) ─────────────────────
# Нативное приложение НЕ может использовать веб-виджет Telegram (origin WKWebView
# не проходит проверку домена бота). Поэтому: приложение создаёт nonce → открывает
# бота `?start=app_<nonce>` → бот подтверждает (привязывает chat_id+сессию) →
# приложение опрашивает и забирает токен. Nonce живёт 10 минут, одноразовый.
def _applogin_ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS applogin_nonces ("
        " nonce TEXT PRIMARY KEY, created_at TEXT, chat_id INTEGER,"
        " token TEXT, consumed INTEGER DEFAULT 0)"
    )


def applogin_create(nonce: str):
    with _db() as conn:
        _applogin_ensure(conn)
        conn.execute(
            "INSERT INTO applogin_nonces "
            "(nonce, created_at, chat_id, token, consumed) VALUES (?, ?, NULL, NULL, 0) "
            "ON CONFLICT (nonce) DO UPDATE SET created_at=excluded.created_at, "
            "  chat_id=NULL, token=NULL, consumed=0",
            (nonce, datetime.now().isoformat(timespec="seconds")),
        )


def applogin_authorize(nonce: str, chat_id: int, token: str) -> bool:
    """Бот подтвердил вход: привязывает chat_id+сессию к свежему неиспользованному nonce."""
    fresh = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    with _db() as conn:
        _applogin_ensure(conn)
        cur = conn.execute(
            "UPDATE applogin_nonces SET chat_id = ?, token = ? "
            "WHERE nonce = ? AND consumed = 0 AND token IS NULL AND created_at >= ?",
            (int(chat_id), token, nonce, fresh),
        )
        return cur.rowcount > 0


def applogin_poll(nonce: str) -> dict:
    """Опрос из приложения. status: pending | ready(+token,chat_id) | expired. ready — один раз."""
    fresh = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    with _db() as conn:
        _applogin_ensure(conn)
        row = conn.execute(
            "SELECT chat_id, token, consumed, created_at FROM applogin_nonces WHERE nonce = ?",
            (nonce,),
        ).fetchone()
        if not row or row["created_at"] < fresh or row["consumed"]:
            return {"status": "expired"}
        if row["token"]:
            conn.execute("UPDATE applogin_nonces SET consumed = 1 WHERE nonce = ?", (nonce,))
            return {"status": "ready", "token": row["token"], "chat_id": row["chat_id"]}
        return {"status": "pending"}


# ─── Согласия на обработку ПД ───────────────────────────────────────────

def save_consent(client_id: int, consent_given: bool, source: str = "telegram",
                  consent_version: str = CONSENT_VERSION):
    """Логирует факт согласия (или отказа) на обработку персональных данных."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO consents (client_id, consent_given, consent_at, "
            "consent_version, source) VALUES (?, ?, ?, ?, ?)",
            (client_id, 1 if consent_given else 0, _now(), consent_version, source),
        )


def set_marketing_consent(client_id: int, consent_given: bool):
    """
    Помечает согласие/отказ на маркетинговые рассылки.
    consent_given=True  → пишем marketing_consent_at = сейчас, чистим _revoked_at.
    consent_given=False → чистим marketing_consent_at, пишем _revoked_at = сейчас.
    """
    with _db() as conn:
        if consent_given:
            conn.execute(
                "UPDATE clients SET marketing_consent_at = ?, "
                "marketing_consent_revoked_at = NULL WHERE id = ?",
                (_now(), client_id),
            )
        else:
            conn.execute(
                "UPDATE clients SET marketing_consent_at = NULL, "
                "marketing_consent_revoked_at = ? WHERE id = ?",
                (_now(), client_id),
            )


def has_marketing_consent(client_id: int) -> bool:
    """True, если клиент согласился на маркетинговые рассылки и не отозвал."""
    with _db() as conn:
        row = conn.execute(
            "SELECT marketing_consent_at FROM clients WHERE id = ?",
            (client_id,),
        ).fetchone()
        if not row:
            return False
        return bool(row["marketing_consent_at"])


def has_marketing_consent_by_chat_id(telegram_chat_id: int) -> bool:
    """То же, но по chat_id (удобно для рассылок)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT marketing_consent_at FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if not row:
            return False
        return bool(row["marketing_consent_at"])


# ─── Персональные настройки уведомлений клиента ──────────────────────────
# Клиент сам регулирует, что и как часто получать. Дефолты = ТЕКУЩЕЕ поведение,
# чтобы никого не «обрезать» молча: настройки применяются, только если клиент
# их менял. marketing-категории дополнительно гейтятся marketing_consent
# (юридическое согласие), эти настройки — тонкая регулировка ВНУТРИ согласия.
import json as _json_np

NOTIFY_PREFS_DEFAULTS = {
    "record_changes": True,    # изменения по моей записи (создана/перенесена/отменена)
    "reminder": True,          # напоминание перед визитом (наш Telegram + YClients SMS)
    "reminder_hours": 3,       # за сколько часов до визита (0..48); рулит notify_by_sms YClients
    "marketing": True,         # акции / промокоды
    "marketing_freq": "week",  # week | 2weeks | month — минимальный интервал между акциями
    "cycle": True,             # «давно не были» (возвращающие)
    "birthday": True,          # промокод на день рождения
    "freed_slot": True,        # «освободилось окно у мастера»
    "quiet_from": None,        # тихие часы: час начала 0..23 или None
    "quiet_to": None,          # тихие часы: час конца 0..23 или None
}
# Минимальный интервал маркетинга в днях
MARKETING_FREQ_DAYS = {"week": 7, "2weeks": 14, "month": 30}


def _notify_prefs_ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notify_prefs ("
        " client_id INTEGER PRIMARY KEY, prefs TEXT, updated_at TEXT)")


def get_notify_prefs(client_id: int) -> dict:
    """Настройки уведомлений клиента, слитые с дефолтами (всегда полный набор ключей)."""
    prefs = dict(NOTIFY_PREFS_DEFAULTS)
    try:
        with _db() as conn:
            _notify_prefs_ensure(conn)
            row = conn.execute(
                "SELECT prefs FROM notify_prefs WHERE client_id = ?",
                (int(client_id),)).fetchone()
        if row and row["prefs"]:
            saved = _json_np.loads(row["prefs"])
            if isinstance(saved, dict):
                for k in NOTIFY_PREFS_DEFAULTS:
                    if k in saved:
                        prefs[k] = saved[k]
    except Exception:
        pass
    return prefs


def get_notify_prefs_by_chat_id(telegram_chat_id: int) -> dict:
    """Настройки по Telegram chat_id (для отправщиков уведомлений)."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT id FROM clients WHERE telegram_chat_id = ?",
                (int(telegram_chat_id),)).fetchone()
        if row:
            return get_notify_prefs(row["id"])
    except Exception:
        pass
    return dict(NOTIFY_PREFS_DEFAULTS)


def set_notify_prefs(client_id: int, partial: dict) -> dict:
    """Частичное обновление настроек (мержим с текущими). Возвращает итог."""
    cur = get_notify_prefs(client_id)
    for k, v in (partial or {}).items():
        if k not in NOTIFY_PREFS_DEFAULTS:
            continue
        if k in ("reminder_hours",):
            try:
                v = max(0, min(48, int(v)))
            except (TypeError, ValueError):
                continue
        elif k in ("quiet_from", "quiet_to"):
            if v is None or v == "":
                v = None
            else:
                try:
                    v = max(0, min(23, int(v)))
                except (TypeError, ValueError):
                    continue
        elif k == "marketing_freq":
            if v not in MARKETING_FREQ_DAYS:
                continue
        else:
            v = bool(v)
        cur[k] = v
    with _db() as conn:
        _notify_prefs_ensure(conn)
        conn.execute(
            "INSERT INTO notify_prefs (client_id, prefs, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (client_id) DO UPDATE SET "
            "  prefs=excluded.prefs, updated_at=excluded.updated_at",
            (int(client_id), _json_np.dumps(cur, ensure_ascii=False), _now()))
    return cur


def in_quiet_hours(prefs: dict, now_hour: int) -> bool:
    """True, если текущий час попадает в тихие часы клиента (не маркетинг/не срочное)."""
    try:
        qf, qt = prefs.get("quiet_from"), prefs.get("quiet_to")
        if qf is None or qt is None:
            return False
        qf, qt, h = int(qf), int(qt), int(now_hour)
        if qf == qt:
            return False
        if qf < qt:
            return qf <= h < qt
        return h >= qf or h < qt   # окно через полночь (напр. 22→9)
    except Exception:
        return False


def has_saved_notify_prefs(client_id: int) -> bool:
    """True, если клиент РЕАЛЬНО открывал «Настройки» и что-то сохранил (есть строка).
    Нужно, чтобы частотный троттл маркетинга применялся ТОЛЬКО к тем, кто сам выбрал
    частоту — иначе дефолтный 7-дневный кап молча резал бы рассылки всей базе."""
    try:
        with _db() as conn:
            _notify_prefs_ensure(conn)
            row = conn.execute(
                "SELECT 1 FROM notify_prefs WHERE client_id = ? LIMIT 1",
                (int(client_id),)).fetchone()
        return bool(row)
    except Exception:
        return False


# Троттлинг частоты маркетинга: запоминаем момент последней отправки клиенту,
# чтобы уважать его marketing_freq (week|2weeks|month). Самомигрирующаяся таблица.
def _marketing_last_ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS client_marketing_last ("
        " client_id INTEGER PRIMARY KEY, sent_at TEXT)")


def set_marketing_last_sent(client_id: int):
    """Запомнить момент успешной отправки маркетингового сообщения клиенту."""
    with _db() as conn:
        _marketing_last_ensure(conn)
        conn.execute(
            "INSERT INTO client_marketing_last (client_id, sent_at) "
            "VALUES (?, ?) "
            "ON CONFLICT (client_id) DO UPDATE SET sent_at=excluded.sent_at",
            (int(client_id), _now()))


def marketing_sent_within(client_id: int, days: int) -> bool:
    """True, если маркетинг этому клиенту слали в последние N дней."""
    if days <= 0:
        return False
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        _marketing_last_ensure(conn)
        row = conn.execute(
            "SELECT 1 FROM client_marketing_last "
            "WHERE client_id = ? AND sent_at > ? LIMIT 1",
            (int(client_id), cutoff)).fetchone()
        return bool(row)


def has_valid_consent(client_id: int, consent_version: str = CONSENT_VERSION) -> bool:
    """True, если у клиента есть действующее согласие текущей версии."""
    with _db() as conn:
        row = conn.execute(
            "SELECT consent_given FROM consents "
            "WHERE client_id = ? AND consent_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (client_id, consent_version),
        ).fetchone()
        return bool(row and row["consent_given"])


def has_valid_consent_by_chat_id(telegram_chat_id: int) -> bool:
    """True, если по этому Telegram-аккаунту есть подписанное согласие на ПД.

    Используется глобальным гейтом до того, как пользователь как-либо
    взаимодействовал с ботом (записи у него ещё нет → можно только
    проверить, есть ли запись в clients и было ли подписано согласие).
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if not row:
            return False
        return has_valid_consent(row["id"])


def has_made_marketing_decision_by_chat_id(telegram_chat_id: int) -> bool:
    """True, если пользователь явно сделал выбор по маркетинговому согласию
    (либо принял, либо отказался — главное, что вопрос ему задавали и
    он на него ответил)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT marketing_consent_at, marketing_consent_revoked_at "
            "FROM clients WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if not row:
            return False
        return bool(row["marketing_consent_at"] or row["marketing_consent_revoked_at"])


def consent_gate_status(telegram_chat_id: int) -> str:
    """Сводный статус «все ли документы подписаны» для глобального гейта.

    Возвращает:
      'pass'           — ПД и маркетинг (любой выбор) пройдены, бот доступен
      'need_pdn'       — нужно подписать согласие на ПД
      'need_marketing' — ПД подписано, но решение по рассылке ещё не сделано
    """
    if not has_valid_consent_by_chat_id(telegram_chat_id):
        return "need_pdn"
    if not has_made_marketing_decision_by_chat_id(telegram_chat_id):
        return "need_marketing"
    return "pass"


def export_consents_csv() -> str:
    """
    Возвращает CSV-выгрузку согласий клиентов на обработку ПД + маркетинговых
    согласий для аудита / Роскомнадзора.
    """
    import csv, io as _io
    out = _io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "тип_согласия", "client_id", "telegram_chat_id", "имя",
        "согласие", "дата_время", "версия", "источник",
    ])
    with _db() as conn:
        # Базовые согласия на ПД
        rows = conn.execute("""
            SELECT 'ПД (имя+телефон)' AS kind, c.client_id, cl.telegram_chat_id,
                   cl.name_enc, c.consent_given, c.consent_at,
                   c.consent_version, c.source
            FROM consents c
            LEFT JOIN clients cl ON cl.id = c.client_id
            ORDER BY c.consent_at
        """).fetchall()
        for r in rows:
            name = ""
            try:
                from pii_crypto import decrypt
                if r["name_enc"]:
                    name = decrypt(r["name_enc"]) or ""
            except Exception:
                name = "[не расшифровать]"
            w.writerow([
                r["kind"], r["client_id"], r["telegram_chat_id"] or "",
                name, "да" if r["consent_given"] else "нет",
                r["consent_at"], r["consent_version"], r["source"],
            ])

        # Маркетинговые согласия — из clients.marketing_consent_at/_revoked_at
        rows = conn.execute("""
            SELECT id, telegram_chat_id, name_enc,
                   marketing_consent_at, marketing_consent_revoked_at
            FROM clients
            WHERE marketing_consent_at IS NOT NULL
               OR marketing_consent_revoked_at IS NOT NULL
        """).fetchall()
        for r in rows:
            name = ""
            try:
                from pii_crypto import decrypt
                if r["name_enc"]:
                    name = decrypt(r["name_enc"]) or ""
            except Exception:
                name = "[не расшифровать]"
            if r["marketing_consent_at"]:
                w.writerow([
                    "Маркетинговые рассылки", r["id"], r["telegram_chat_id"] or "",
                    name, "да", r["marketing_consent_at"], "v1.0", "telegram",
                ])
            if r["marketing_consent_revoked_at"]:
                w.writerow([
                    "Маркетинг — отозвано", r["id"], r["telegram_chat_id"] or "",
                    name, "нет (отозвано)", r["marketing_consent_revoked_at"],
                    "v1.0", "telegram",
                ])
    return out.getvalue()


# ─── Записи ─────────────────────────────────────────────────────────────

def save_booking(client_id: int, service: str, master: str,
                 datetime_str: str, yclients_record_id: int = None):
    """Сохраняет оформленную запись клиента."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO bookings (client_id, service, master, datetime, "
            "yclients_record_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, service, master, datetime_str, yclients_record_id, _now()),
        )


def get_last_booking(telegram_chat_id: int) -> dict | None:
    """
    Последняя запись клиента — для обезличенного контекста.
    Возвращает только услугу/мастера/дату, без персональных данных.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT b.service, b.master, b.datetime FROM bookings b "
            "JOIN clients c ON c.id = b.client_id "
            "WHERE c.telegram_chat_id = ? ORDER BY b.id DESC LIMIT 1",
            (telegram_chat_id,),
        ).fetchone()
        return dict(row) if row else None


# ─── Подарочные сертификаты ────────────────────────────────────────────

import secrets

_CERT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O/1/I/L — чтобы не путать на глаз


def new_cert_code(amount: int) -> str:
    """Генерирует уникальный код сертификата вида MEC-<сумма>-<6 символов>."""
    suffix = "".join(secrets.choice(_CERT_ALPHABET) for _ in range(6))
    return f"MEC-{amount}-{suffix}"


def _cert_row_to_dict(row) -> dict:
    """Расшифровывает recipient_name/phone из строки certs."""
    d = dict(row)
    d["recipient_name"] = pii_crypto.decrypt(d.get("recipient_name_enc"))
    d["recipient_phone"] = pii_crypto.decrypt(d.get("recipient_phone_enc"))
    return d


def save_gift_certificate(
    code: str,
    amount: int,
    recipient_phone: str,
    expires_at: str,
    recipient_name: str = None,
    buyer_chat_id: int = None,
    yukassa_payment_id: str = None,
    payment_status: str = "paid",
):
    """Сохраняет выпущенный сертификат. Имя/телефон получателя шифрует."""
    with _db() as conn:
        # Legacy-столбец recipient_phone имеет NOT NULL — пишем пустую строку,
        # реальный (зашифрованный) телефон уходит в recipient_phone_enc.
        conn.execute(
            "INSERT INTO gift_certificates ("
            "code, amount, recipient_phone, recipient_name, "
            "recipient_phone_enc, recipient_phone_hash, recipient_name_enc, "
            "buyer_chat_id, issued_at, expires_at, payment_status, yukassa_payment_id"
            ") VALUES (?, ?, '', NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code, amount,
                pii_crypto.encrypt(recipient_phone),
                pii_crypto.hash_phone(recipient_phone),
                pii_crypto.encrypt(recipient_name),
                buyer_chat_id, _now(), expires_at, payment_status, yukassa_payment_id,
            ),
        )


def get_gift_certificate(code: str) -> dict | None:
    """Возвращает сертификат по коду с расшифрованными ПД получателя."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM gift_certificates WHERE code = ?",
            (code,),
        ).fetchone()
        return _cert_row_to_dict(row) if row else None


def mark_cert_paid(code: str, yukassa_payment_id: str = None) -> bool:
    """
    Помечает сертификат оплаченным (после успешного платежа в ЮKassa).
    Возвращает True, если переход был выполнен (был pending → стал paid).
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE gift_certificates SET payment_status = 'paid', "
            "yukassa_payment_id = ? WHERE code = ? AND payment_status = 'pending'",
            (yukassa_payment_id, code),
        )
        return cur.rowcount > 0


def set_cert_payment_id(code: str, yukassa_payment_id: str) -> bool:
    """
    Привязывает ID платежа ЮKassa к сертификату (сразу после create_payment).
    Нужно, чтобы при рестарте бота можно было возобновить опрос статуса.
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE gift_certificates SET yukassa_payment_id = ? WHERE code = ?",
            (yukassa_payment_id, code),
        )
        return cur.rowcount > 0


def list_pending_certs() -> list[dict]:
    """
    Все сертификаты в статусе pending с уже созданным платежом ЮKassa.
    Используется при старте бота, чтобы возобновить фоновый опрос статуса
    платежей, по которым клиент мог оплатить, пока бот был перезапущен.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM gift_certificates WHERE payment_status = 'pending' "
            "AND yukassa_payment_id IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_cert_canceled(code: str) -> bool:
    """Помечает сертификат отменённым (если ЮKassa вернул status=canceled)."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE gift_certificates SET payment_status = 'canceled' "
            "WHERE code = ? AND payment_status = 'pending'",
            (code,),
        )
        return cur.rowcount > 0


def mark_cert_used(code: str, admin_user_id: int) -> bool:
    """
    Помечает сертификат использованным. Возвращает True, если успешно
    (т.е. сертификат был активен), False если уже погашен / не найден.
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE gift_certificates SET used_at = ?, used_by_admin_id = ? "
            "WHERE code = ? AND used_at IS NULL",
            (_now(), admin_user_id, code),
        )
        return cur.rowcount > 0


def list_certs_by_phone(phone: str) -> list[dict]:
    """Все сертификаты для указанного телефона получателя (поиск по HMAC-хешу)."""
    phone_hash = pii_crypto.hash_phone(phone)
    if not phone_hash:
        return []
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM gift_certificates WHERE recipient_phone_hash = ? "
            "ORDER BY issued_at DESC",
            (phone_hash,),
        ).fetchall()
        return [_cert_row_to_dict(r) for r in rows]


# ─── Админы (могут гасить сертификаты) ─────────────────────────────────

def add_admin(telegram_user_id: int, added_by: int = None) -> bool:
    """Добавляет админа (идемпотентно). True если добавлен сейчас, False если уже был."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO admins (telegram_user_id, added_at, added_by_user_id) "
            "VALUES (?, ?, ?)",
            (telegram_user_id, _now(), added_by),
        )
        return cur.rowcount > 0


def is_admin(telegram_user_id: int) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return bool(row)


def can_redeem_codes(telegram_user_id: int) -> bool:
    """
    True если пользователь имеет право гасить коды (баллы лояльности,
    сертификаты): он либо админ, либо привязанный мастер с can_redeem=1.
    """
    if is_admin(telegram_user_id):
        return True
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM masters_telegram WHERE telegram_chat_id = ? "
            "AND can_redeem = 1 LIMIT 1",
            (telegram_user_id,),
        ).fetchone()
        return bool(row)


def list_cashiers() -> list[dict]:
    """Список мастеров с правом гасить коды (роль «кассир»)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM masters_telegram WHERE can_redeem = 1 "
            "ORDER BY full_name"
        ).fetchall()
        return [dict(r) for r in rows]


def set_cashier_role(yclients_staff_id: int, can_redeem: bool) -> bool:
    """Включает/выключает роль кассира у мастера. True если строка нашлась."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE masters_telegram SET can_redeem = ? "
            "WHERE yclients_staff_id = ?",
            (1 if can_redeem else 0, yclients_staff_id),
        )
        return cur.rowcount > 0


def find_master_by_partial_name(query: str) -> dict | None:
    """
    Ищет мастера по частичному совпадению имени (case-insensitive).
    Используется в админ-команде `/cashier_grant Стас`.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM masters_telegram WHERE LOWER(full_name) LIKE ?",
            (f"%{q}%",),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None


def list_admins() -> list[int]:
    with _db() as conn:
        rows = conn.execute("SELECT telegram_user_id FROM admins ORDER BY id").fetchall()
        return [r["telegram_user_id"] for r in rows]


def remove_admin(telegram_user_id: int) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM admins WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        return cur.rowcount > 0


# ─── Мастера: bind-коды, привязка, mute ────────────────────────────────

import json
from datetime import timedelta


def create_master_with_bind_code(yclients_staff_id: int, full_name: str) -> str:
    """
    Регистрирует мастера в системе и генерирует bind-код. Идемпотентно:
    если мастер уже есть и ещё не привязан — возвращает существующий код.
    Если уже привязан — возвращает None.
    """
    with _db() as conn:
        existing = conn.execute(
            "SELECT bind_code, telegram_chat_id FROM masters_telegram "
            "WHERE yclients_staff_id = ?",
            (yclients_staff_id,),
        ).fetchone()
        if existing:
            if existing["telegram_chat_id"]:
                return None  # уже привязан
            return existing["bind_code"]

        # Генерируем уникальный код вида ME-XXXXXX
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        for _ in range(10):
            code = "ME-" + "".join(secrets.choice(alphabet) for _ in range(6))
            try:
                conn.execute(
                    "INSERT INTO masters_telegram "
                    "(yclients_staff_id, full_name, bind_code, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (yclients_staff_id, full_name, code, _now()),
                )
                return code
            except psycopg2.IntegrityError:
                continue
        raise RuntimeError("Не удалось сгенерировать уникальный bind-код")


def reset_master_bind_code(yclients_staff_id: int, full_name: str) -> str:
    """
    Генерирует НОВЫЙ bind-код для мастера, сбрасывая текущую привязку.
    Создаёт запись, если её ещё не было. Используется когда админ просит
    «выдать новый код» (мастер сменил телефон, потерял доступ и т.п.).
    Возвращает новый код.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM masters_telegram WHERE yclients_staff_id = ?",
            (yclients_staff_id,),
        ).fetchone()
        for _ in range(10):
            code = "ME-" + "".join(secrets.choice(alphabet) for _ in range(6))
            try:
                if existing:
                    conn.execute(
                        "UPDATE masters_telegram SET bind_code = ?, "
                        "telegram_chat_id = NULL, bound_at = NULL, "
                        "full_name = ?, is_active = 1 "
                        "WHERE yclients_staff_id = ?",
                        (code, full_name, yclients_staff_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO masters_telegram "
                        "(yclients_staff_id, full_name, bind_code, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (yclients_staff_id, full_name, code, _now()),
                    )
                return code
            except psycopg2.IntegrityError:
                continue
        raise RuntimeError("Не удалось сгенерировать уникальный bind-код")


def bind_master(bind_code: str, telegram_chat_id: int) -> dict | None:
    """
    Привязывает Telegram-аккаунт к мастеру по bind-коду.
    Возвращает запись мастера при успехе, None — если код невалидный или уже использован.
    """
    code = (bind_code or "").strip().upper()
    with _db() as conn:
        row = conn.execute(
            "SELECT id, yclients_staff_id, full_name, telegram_chat_id "
            "FROM masters_telegram WHERE bind_code = ?",
            (code,),
        ).fetchone()
        if not row:
            return None
        # Уже привязан кем-то другим
        if row["telegram_chat_id"] and row["telegram_chat_id"] != telegram_chat_id:
            return None
        # Этот же мастер уже привязан к этому же chat_id — успех (идемпотентно)
        if row["telegram_chat_id"] == telegram_chat_id:
            return dict(row)
        # Свободный код — привязываем
        conn.execute(
            "UPDATE masters_telegram SET telegram_chat_id = ?, bound_at = ?, is_active = 1 "
            "WHERE id = ?",
            (telegram_chat_id, _now(), row["id"]),
        )
        result = dict(row)
        result["telegram_chat_id"] = telegram_chat_id
        return result


def unbind_master(telegram_chat_id: int) -> bool:
    """Отвязывает мастера от Telegram-аккаунта. True если был привязан."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE masters_telegram SET telegram_chat_id = NULL, bound_at = NULL "
            "WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        )
        return cur.rowcount > 0


def get_master_by_chat_id(telegram_chat_id: int) -> dict | None:
    """Возвращает запись мастера по Telegram chat_id, или None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM masters_telegram WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        return dict(row) if row else None


def get_master_by_staff_id(yclients_staff_id: int) -> dict | None:
    """Возвращает мастера по YClients staff_id, или None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM masters_telegram WHERE yclients_staff_id = ?",
            (yclients_staff_id,),
        ).fetchone()
        return dict(row) if row else None


def list_masters() -> list[dict]:
    """Все мастера, включая непривязанных — для админ-обзора."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM masters_telegram ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Внутренний чат сотрудников (общий канал команды) ───────────────────────
# Человек-человек, БЕЗ ИИ. Сообщения хранятся локально; на новое сообщение
# бэкенд шлёт пуш (Telegram + Web Push) остальным сотрудникам.

# Колонки сообщения (id + автор + текст + вложение). Один список — чтобы оба
# SELECT'а и INSERT не разъезжались.
_STAFF_MSG_SELECT = ("id, sender_chat_id, sender_name, text, created_at, "
                     "media_kind, media_url, media_name, media_mime, media_size, media_dur")


def _staff_messages_ensure(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS staff_messages ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  sender_chat_id INTEGER NOT NULL,"
        "  sender_name TEXT,"
        "  text        TEXT NOT NULL,"
        "  created_at  TEXT NOT NULL,"
        "  media_kind  TEXT,"      # '' | 'image' | 'video' | 'voice' | 'file'
        "  media_url   TEXT,"      # публичная ссылка на файл (на Beget)
        "  media_name  TEXT,"      # исходное имя файла
        "  media_mime  TEXT,"
        "  media_size  INTEGER,"   # размер файла в байтах
        "  media_dur   REAL"       # длительность (сек) для голоса/видео
        ")"
    )
    # Идемпотентная миграция: в проде таблица уже создана (старая 5-колоночная),
    # а CREATE IF NOT EXISTS колонок не добавляет — дописываем по одной.
    for _col, _typ in (("media_kind", "TEXT"), ("media_url", "TEXT"),
                       ("media_name", "TEXT"), ("media_mime", "TEXT"),
                       ("media_size", "INTEGER"), ("media_dur", "REAL")):
        try:
            conn.execute(f"ALTER TABLE staff_messages ADD COLUMN {_col} {_typ}")
        except Exception:
            pass  # колонка уже есть


def add_staff_message(sender_chat_id: int, sender_name: str, text: str,
                      media_kind: str = "", media_url: str = "", media_name: str = "",
                      media_mime: str = "", media_size: int = 0,
                      media_dur: float = 0) -> int:
    """Сохраняет сообщение команды (текст и/или вложение), возвращает его id."""
    with _db() as conn:
        _staff_messages_ensure(conn)
        cur = conn.execute(
            "INSERT INTO staff_messages "
            "(sender_chat_id, sender_name, text, created_at, "
            " media_kind, media_url, media_name, media_mime, media_size, media_dur) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(sender_chat_id), str(sender_name or "")[:80], str(text or "")[:2000],
             datetime.now().isoformat(timespec="seconds"),
             str(media_kind or "")[:16], str(media_url or "")[:512],
             str(media_name or "")[:200], str(media_mime or "")[:80],
             int(media_size or 0), float(media_dur or 0)),
        )
        return int(cur.lastrowid)


def get_staff_messages_since(since_id: int = 0, limit: int = 100) -> list[dict]:
    """Сообщения новее since_id (для поллинга открытого чата)."""
    with _db() as conn:
        _staff_messages_ensure(conn)
        rows = conn.execute(
            f"SELECT {_STAFF_MSG_SELECT} FROM staff_messages "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(since_id or 0), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def get_staff_messages_recent(limit: int = 50) -> list[dict]:
    """Последние N сообщений в хронологическом порядке (первая загрузка чата)."""
    with _db() as conn:
        _staff_messages_ensure(conn)
        rows = conn.execute(
            f"SELECT {_STAFF_MSG_SELECT} FROM staff_messages "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))


def delete_staff_message(message_id: int, sender_chat_id: int) -> dict:
    """Удаляет своё сообщение команды. Чужие сообщения не трогает."""
    with _db() as conn:
        _staff_messages_ensure(conn)
        row = conn.execute(
            f"SELECT {_STAFF_MSG_SELECT} FROM staff_messages WHERE id = ?",
            (int(message_id or 0),),
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        msg = dict(row)
        if int(msg.get("sender_chat_id") or 0) != int(sender_chat_id or 0):
            return {"ok": False, "reason": "forbidden"}
        conn.execute(
            "DELETE FROM staff_messages WHERE id = ? AND sender_chat_id = ?",
            (int(message_id), int(sender_chat_id)),
        )
        return {"ok": True, "message": msg}


def mute_master(telegram_chat_id: int, hours: float) -> bool:
    """Заглушает уведомления для мастера на N часов. False если мастер не найден."""
    until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    with _db() as conn:
        cur = conn.execute(
            "UPDATE masters_telegram SET mute_until = ? WHERE telegram_chat_id = ?",
            (until, telegram_chat_id),
        )
        return cur.rowcount > 0


def unmute_master(telegram_chat_id: int) -> bool:
    """Снимает mute с мастера."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE masters_telegram SET mute_until = NULL WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        )
        return cur.rowcount > 0


def is_master_muted(telegram_chat_id: int) -> bool:
    """True, если у мастера сейчас активен mute."""
    with _db() as conn:
        row = conn.execute(
            "SELECT mute_until FROM masters_telegram WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if not row or not row["mute_until"]:
            return False
        try:
            return datetime.fromisoformat(row["mute_until"]) > datetime.now()
        except Exception:
            return False


# ─── Кто инициировал перенос записи (эфемерно, для webhook-уведомления мастеру) ──
#
# YClients в webhook НЕ сообщает, кто перенёс запись. Но когда перенос инициируем
# МЫ (клиент через бота MAYA / владелец-мастер через панель), наш код знает актора.
# Ставим короткоживущую метку record_id→actor, и обработчик record.update её читает,
# чтобы написать мастеру «Клиент перенёс сам» vs «Перенесено администратором».
import time as _time_resched

_RESCHEDULE_ACTORS: dict = {}   # record_id -> (timestamp, "client"|"staff")


def mark_reschedule_actor(record_id: int, actor: str) -> None:
    """Запомнить, кто инициировал НАШ перенос записи. actor: 'client' | 'staff'."""
    try:
        now = _time_resched.time()
        # лёгкая чистка протухших меток, чтобы словарь не рос бесконечно
        if len(_RESCHEDULE_ACTORS) > 200:
            for k in [k for k, (ts, _a) in list(_RESCHEDULE_ACTORS.items()) if now - ts > 600]:
                _RESCHEDULE_ACTORS.pop(k, None)
        _RESCHEDULE_ACTORS[int(record_id)] = (now, str(actor))
    except Exception:
        pass


def pop_recent_reschedule_actor(record_id: int, max_age: float = 180.0):
    """Вернуть актора недавнего НАШЕГО переноса и удалить метку. None если нет/протухло."""
    try:
        rec = _RESCHEDULE_ACTORS.pop(int(record_id), None)
        if not rec:
            return None
        ts, actor = rec
        return actor if (_time_resched.time() - ts) <= max_age else None
    except Exception:
        return None


# ─── Кто отменил запись (эфемерно, для webhook-уведомления мастеру) ──────────
# YClients в webhook record.delete НЕ сообщает, кто отменил. Когда отмену
# инициирует КЛИЕНТ через нашего бота («Мои записи → Отменить» / запрос к MAYA),
# ставим метку record_id→'client'; обработчик record.delete её читает и пишет
# мастеру «Запись отменена клиентом» вместо обезличенного «Запись отменена».
_CANCEL_ACTORS: dict = {}   # record_id -> (timestamp, "client"|"staff")


def mark_cancel_actor(record_id: int, actor: str) -> None:
    """Запомнить, кто инициировал НАШУ отмену записи. actor: 'client' | 'staff'."""
    try:
        now = _time_resched.time()
        if len(_CANCEL_ACTORS) > 200:
            for k in [k for k, (ts, _a) in list(_CANCEL_ACTORS.items()) if now - ts > 900]:
                _CANCEL_ACTORS.pop(k, None)
        _CANCEL_ACTORS[int(record_id)] = (now, str(actor))
    except Exception:
        pass


def pop_recent_cancel_actor(record_id: int, max_age: float = 300.0):
    """Вернуть актора недавней НАШЕЙ отмены и удалить метку. None если нет/протухло."""
    try:
        rec = _CANCEL_ACTORS.pop(int(record_id), None)
        if not rec:
            return None
        ts, actor = rec
        return actor if (_time_resched.time() - ts) <= max_age else None
    except Exception:
        return None


# ─── Webhook-дедупликация ──────────────────────────────────────────────

def mark_record_processed(record_id: int, event_type: str = "record.create") -> bool:
    """
    Помечает webhook-событие обработанным. Возвращает True, если это первый раз
    (нужно обработать), False — если уже было (дубликат, игнорируем).
    """
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_records (record_id, event_type, processed_at) "
                "VALUES (?, ?, ?)",
                (record_id, event_type, _now()),
            )
            return True
        except psycopg2.IntegrityError:
            return False


# ─── Снимок состояния записи (для детекции изменений на webhook update) ──

def get_record_state(record_id: int) -> dict | None:
    """Последнее известное состояние записи или None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM record_state WHERE record_id = ?", (record_id,)
        ).fetchone()
        return dict(row) if row else None


def upsert_record_state(
    record_id: int,
    staff_id: int | None,
    datetime_str: str | None,
    services_sig: str | None,
    attendance: int | None,
):
    """Сохраняет/обновляет снимок состояния записи."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO record_state "
            "(record_id, staff_id, datetime, services_sig, attendance, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, record_id) DO UPDATE SET "
            "  staff_id = excluded.staff_id, "
            "  datetime = excluded.datetime, "
            "  services_sig = excluded.services_sig, "
            "  attendance = excluded.attendance, "
            "  updated_at = excluded.updated_at",
            (record_id, staff_id, datetime_str, services_sig,
             attendance, _now()),
        )


def delete_record_state(record_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM record_state WHERE record_id = ?", (record_id,))


# ─── Настроение визита (🔴 тишина / 🔵 общение) ─────────────────────────

_VALID_MOODS = ("red", "blue")


def set_visit_mood(record_id: int, mood: str, source: str = "bot",
                   client_id: int | None = None) -> bool:
    """Сохраняет выбор настроения для конкретной записи (idempotent upsert).
    Также запоминает выбор как default клиента для будущих записей.
    Возвращает True, если mood валиден и сохранён."""
    mood = (mood or "").strip().lower()
    if mood not in _VALID_MOODS:
        return False
    _ts = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO visit_mood "
            "(record_id, mood, source, client_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id) DO UPDATE SET "
            "  mood = excluded.mood, "
            "  source = excluded.source, "
            "  client_id = COALESCE(excluded.client_id, visit_mood.client_id), "
            "  updated_at = excluded.updated_at",
            (record_id, mood, source, client_id, _ts, _ts),
        )
        if client_id:
            try:
                conn.execute(
                    "UPDATE clients SET default_visit_mood = ? WHERE id = ?",
                    (mood, client_id),
                )
            except psycopg2.OperationalError:
                pass  # колонка ещё не мигрирована — не критично
    return True


def get_visit_mood(record_id: int) -> str | None:
    """'red' | 'blue' | None для конкретной записи."""
    with _db() as conn:
        row = conn.execute(
            "SELECT mood FROM visit_mood WHERE record_id = ?", (record_id,)
        ).fetchone()
        return row["mood"] if row else None


def get_visit_moods(record_ids) -> dict:
    """Батч-чтение настроений для списка record_id → {record_id: mood}.
    Используется журналом, чтобы не делать N запросов на день расписания."""
    ids = []
    for r in record_ids:
        if r is None:
            continue
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            continue  # пропускаем один кривой id, не теряя настроения остальных
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT record_id, mood FROM visit_mood WHERE record_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {row["record_id"]: row["mood"] for row in rows}


def get_default_visit_mood(client_id: int) -> str | None:
    """Последний выбор клиента ('red'|'blue'|None) — для пред-выбора при записи."""
    if not client_id:
        return None
    with _db() as conn:
        try:
            row = conn.execute(
                "SELECT default_visit_mood FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
        except psycopg2.OperationalError:
            return None
        return row["default_visit_mood"] if row and row["default_visit_mood"] else None


def delete_visit_mood(record_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM visit_mood WHERE record_id = ?", (record_id,))


# ─── Кеш истории клиентов (24ч) ────────────────────────────────────────

def get_client_history_cached(client_id: int, max_age_hours: int = 24) -> list | None:
    """Возвращает закешированную историю клиента, если она ещё свежая, иначе None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT history_json, updated_at FROM client_history_cache WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if not row:
            return None
        try:
            age = datetime.now() - datetime.fromisoformat(row["updated_at"])
            if age.total_seconds() > max_age_hours * 3600:
                return None
            return json.loads(row["history_json"])
        except Exception:
            return None


def set_client_history_cache(client_id: int, history: list):
    """Сохраняет историю клиента в кеш (UPSERT)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO client_history_cache "
            "(client_id, history_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id, client_id) DO UPDATE SET "
            "  history_json=excluded.history_json, updated_at=excluded.updated_at",
            (client_id, json.dumps(history, ensure_ascii=False), _now()),
        )


# ─── Лог AI-советов ────────────────────────────────────────────────────

def log_ai_advice(
    record_id: int,
    client_id: int | None,
    staff_id: int,
    ai_provider: str,
    advice_text: str | None,
    initial_services: list[str] | None = None,
) -> int:
    """Записывает факт выдачи AI-совета. Возвращает id записи в логе."""
    import json as _json
    initial_json = _json.dumps(initial_services or [], ensure_ascii=False)
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO ai_advice_log (record_id, client_id, staff_id, "
            "ai_provider, advice_text, initial_services_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record_id, client_id, staff_id, ai_provider, advice_text,
             initial_json, _now()),
        )
        return cur.lastrowid


def update_ai_advice_final_services(record_id: int, final_services: list[str]):
    """Сохраняет фактический состав услуг визита (для анализа «зашёл совет»)."""
    import json as _json
    with _db() as conn:
        conn.execute(
            "UPDATE ai_advice_log SET final_services_json = ? WHERE record_id = ?",
            (_json.dumps(final_services or [], ensure_ascii=False), record_id),
        )


def ai_advice_stats(since_iso: str | None = None) -> dict:
    """
    Сводка по логу AI-советов.

    Возвращает:
      • total — всего советов выдано
      • by_provider — разбивка по моделям (claude/openai/fallback)
      • closed — сколько записей закрыто кнопкой (cash/card)
      • avg_check_closed — средний чек закрытых записей с советом
      • upsell_grew — у скольких записей итоговый состав услуг больше начального
      • upsell_rate_pct — % от закрытых, где состав вырос
      • by_staff — топ-10 мастеров по числу советов
    """
    import json as _json
    q_filter = "WHERE 1=1"
    params: list = []
    if since_iso:
        q_filter += " AND created_at >= ?"
        params.append(since_iso)

    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM ai_advice_log {q_filter}", params,
        ).fetchone()["c"]
        by_provider = {
            r["ai_provider"]: int(r["c"]) for r in conn.execute(
                f"SELECT COALESCE(ai_provider, 'unknown') AS ai_provider, "
                f"COUNT(*) AS c FROM ai_advice_log {q_filter} "
                f"GROUP BY ai_provider", params,
            ).fetchall()
        }
        closed_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM ai_advice_log {q_filter} "
            f"AND button_pressed IS NOT NULL", params,
        ).fetchone()["c"]
        avg_check_row = conn.execute(
            f"SELECT AVG(final_check_amount) AS a FROM ai_advice_log {q_filter} "
            f"AND final_check_amount IS NOT NULL", params,
        ).fetchone()
        avg_check = int(avg_check_row["a"]) if avg_check_row["a"] else 0

        # «Состав услуг вырос» — у закрытых записей сравниваем initial vs final
        rows = conn.execute(
            f"SELECT initial_services_json, final_services_json "
            f"FROM ai_advice_log {q_filter} AND button_pressed IS NOT NULL "
            f"AND initial_services_json IS NOT NULL "
            f"AND final_services_json IS NOT NULL", params,
        ).fetchall()
        upsell_grew = 0
        for r in rows:
            try:
                init_list = _json.loads(r["initial_services_json"] or "[]")
                final_list = _json.loads(r["final_services_json"] or "[]")
                init_set = {(s or "").lower() for s in init_list}
                final_set = {(s or "").lower() for s in final_list}
                if final_set - init_set:
                    upsell_grew += 1
            except Exception:
                continue
        upsell_rate = (upsell_grew / len(rows) * 100) if rows else 0.0

        by_staff = []
        for r in conn.execute(
            f"SELECT staff_id, COUNT(*) AS c FROM ai_advice_log {q_filter} "
            f"GROUP BY staff_id ORDER BY c DESC LIMIT 10", params,
        ).fetchall():
            by_staff.append({"staff_id": r["staff_id"], "count": int(r["c"])})

        return {
            "total": int(total),
            "by_provider": by_provider,
            "closed": int(closed_count),
            "avg_check_closed": avg_check,
            "upsell_grew": upsell_grew,
            "upsell_rate_pct": round(upsell_rate, 1),
            "with_final_services": len(rows),
            "by_staff": by_staff,
        }


def update_ai_advice_outcome(
    record_id: int,
    button_pressed: str,
    payment_method: str | None = None,
    final_check_amount: int | None = None,
):
    """Дописывает результат — что нажал мастер, какой итоговый чек."""
    with _db() as conn:
        conn.execute(
            "UPDATE ai_advice_log SET button_pressed = ?, payment_method = ?, "
            "final_check_amount = ?, closed_at = ? WHERE record_id = ?",
            (button_pressed, payment_method, final_check_amount, _now(), record_id),
        )


def get_ai_advice_for_record(record_id: int) -> dict | None:
    """Свежая запись лога для конкретного record_id (для idempotency-проверки)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM ai_advice_log WHERE record_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None


def get_setting(key: str, default: str | None = None) -> str | None:
    """Возвращает значение настройки или default, если не задана."""
    with _db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    """Сохраняет настройку (UPSERT)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now()),
        )


# ─── Реестр салонов-подписчиков MAYA (founder/GOD-режим) ──────────────────
def list_maya_tenants() -> list[dict]:
    """Все салоны-подписчики (новые первыми)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM maya_tenants ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_maya_tenant(name: str, city: str = "", plan: str = "", owner_name: str = "",
                    phone: str = "", mrr: int = 0, status: str = "pending") -> int:
    """Заводит салон-подписчик. Возвращает id."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO maya_tenants (name, city, plan, status, owner_name, phone, "
            "mrr, created_at, activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, city, plan, status, owner_name, phone, int(mrr or 0), _now(),
             _now() if status == "active" else None),
        )
        return cur.lastrowid


def set_maya_tenant_status(tenant_id: int, status: str) -> bool:
    """Меняет статус салона (active|suspended|pending). Активация ставит activated_at."""
    with _db() as conn:
        if status == "active":
            conn.execute(
                "UPDATE maya_tenants SET status = ?, "
                "activated_at = COALESCE(activated_at, ?) WHERE id = ?",
                (status, _now(), int(tenant_id)),
            )
        else:
            conn.execute(
                "UPDATE maya_tenants SET status = ? WHERE id = ?",
                (status, int(tenant_id)),
            )
        return True


# ─── Расходы по салону (от ассистента Антона) ─────────────────────────────

def add_salon_expense(date: str, item: str, amount: int, source: str = "anton") -> int:
    """Добавляет один расход по салону за дату. amount — рубли (int)."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO salon_expenses (date, item, amount, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, (item or "").strip()[:120], int(round(amount or 0)), source, _now()),
        )
        return cur.lastrowid


def get_salon_expenses(date: str) -> list[dict]:
    """Список расходов по салону за дату: [{id, item, amount}]."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, item, amount FROM salon_expenses WHERE date = ? ORDER BY id",
            (date,),
        ).fetchall()
        return [{"id": r["id"], "item": r["item"], "amount": r["amount"]} for r in rows]


def sum_salon_expenses(date: str) -> int:
    """Сумма расходов по салону за дату."""
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM salon_expenses WHERE date = ?",
            (date,),
        ).fetchone()
        return int(row["s"] or 0)


def clear_salon_expenses(date: str) -> int:
    """Удаляет все расходы за дату (для повторного ввода). Возвращает кол-во удалённых."""
    with _db() as conn:
        cur = conn.execute("DELETE FROM salon_expenses WHERE date = ?", (date,))
        return cur.rowcount or 0


# ─── Касса со слов Антона (для сверки в дневном отчёте) ──────────────────────

def set_cash_log(date: str, total_till: int, day_cash: int, entered_by=None) -> None:
    """Сохраняет/перезаписывает кассу за день: всего налички + наличка за день."""
    try:
        by = int(entered_by) if entered_by else None
    except Exception:
        by = None
    with _db() as conn:
        conn.execute(
            "INSERT INTO cash_log (date, total_till, day_cash, entered_by, ts) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET total_till=excluded.total_till, "
            "day_cash=excluded.day_cash, entered_by=excluded.entered_by, ts=excluded.ts",
            (date, int(total_till), int(day_cash), by, _now()),
        )


def get_cash_log(date: str) -> dict | None:
    """Касса со слов Антона за дату или None."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT date, total_till, day_cash, entered_by, ts FROM cash_log WHERE date = ?",
                (date,),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


# ─── Лист ожидания на занятое время ───────────────────────────────────────

def add_slot_interest(client_id: int, chat_id, staff_id: int, slot_iso: str) -> bool:
    """Запоминаем интерес клиента к занятому слоту. Дубликат (тот же клиент+
    мастер+слот, ещё не уведомлён) — не плодим."""
    slot_iso = (slot_iso or "")[:16]   # 'YYYY-MM-DDTHH:MM'
    if not (client_id and staff_id and len(slot_iso) >= 15):
        return False
    with _db() as conn:
        ex = conn.execute(
            "SELECT 1 FROM slot_waitlist WHERE client_id=? AND staff_id=? "
            "AND slot_datetime=? AND notified_at IS NULL",
            (client_id, staff_id, slot_iso),
        ).fetchone()
        if ex:
            return True
        conn.execute(
            "INSERT INTO slot_waitlist (client_id, chat_id, staff_id, slot_datetime, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, (int(chat_id) if chat_id else None), staff_id, slot_iso, _now()),
        )
        return True


def get_slot_waitlist(staff_id: int, slot_iso: str, tolerance_min: int = 20) -> list[dict]:
    """Клиенты, ждавшие этот (или близкий ±tolerance_min) слот у мастера, кому
    ещё не писали. [{id, client_id, chat_id, slot_datetime}]."""
    from datetime import datetime as _dt
    slot_iso = (slot_iso or "")[:16]
    try:
        target = _dt.fromisoformat(slot_iso)
    except Exception:
        return []
    day = slot_iso[:10]
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, client_id, chat_id, slot_datetime FROM slot_waitlist "
            "WHERE staff_id=? AND notified_at IS NULL AND substr(slot_datetime,1,10)=?",
            (staff_id, day),
        ).fetchall()
    out = []
    for r in rows:
        try:
            t = _dt.fromisoformat(r["slot_datetime"][:16])
        except Exception:
            continue
        if abs((t - target).total_seconds()) <= tolerance_min * 60:
            out.append({"id": r["id"], "client_id": r["client_id"],
                        "chat_id": r["chat_id"], "slot_datetime": r["slot_datetime"]})
    return out


def get_active_waitlist(limit: int = 200) -> list[dict]:
    """Активный лист ожидания: будущие, ещё не уведомлённые слоты. Сырые строки
    (имена клиентов/мастеров резолвит вызывающий через get_client / YClients)."""
    from datetime import datetime as _dt
    now_iso = _dt.now().isoformat(timespec="minutes")
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, client_id, chat_id, staff_id, slot_datetime, created_at "
            "FROM slot_waitlist WHERE notified_at IS NULL AND slot_datetime >= ? "
            "ORDER BY slot_datetime ASC LIMIT ?",
            (now_iso, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_slot_waitlist_notified(ids: list) -> int:
    """Помечаем записи листа ожидания как уведомлённые (чтобы не писать повторно)."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return 0
    with _db() as conn:
        qs = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE slot_waitlist SET notified_at=? WHERE id IN ({qs})",
            tuple([_now()] + list(ids)),
        )
        return cur.rowcount or 0


# ─── Учёт расхода токенов ИИ ─────────────────────────────────────────────

def log_ai_usage(*, feature: str, model: str,
                  input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                  cost_usd: float = 0.0, user_id: int | None = None):
    """Записывает один вызов ИИ в журнал."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO ai_usage_log (at, feature, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), feature, model, int(input_tokens), int(output_tokens),
             int(cache_read_tokens), int(cache_write_tokens),
             float(cost_usd), user_id),
        )


def sum_ai_usage(feature: str | None = None, since: str | None = None) -> float:
    """Суммарная стоимость в $ за фильтрами. None — без фильтра по фиче/дате."""
    q = "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage_log WHERE 1=1"
    params: list = []
    if feature:
        q += " AND feature = ?"
        params.append(feature)
    if since:
        q += " AND at >= ?"
        params.append(since)
    with _db() as conn:
        row = conn.execute(q, params).fetchone()
        return float(row["total"] or 0)


def aggregate_ai_usage_by_feature(since: str | None = None) -> list[dict]:
    """Группировка по feature: [{feature, cost_usd, calls}], сортировка по убыванию."""
    q = ("SELECT feature, SUM(cost_usd) AS cost_usd, COUNT(*) AS calls "
         "FROM ai_usage_log WHERE 1=1")
    params: list = []
    if since:
        q += " AND at >= ?"
        params.append(since)
    q += " GROUP BY feature ORDER BY cost_usd DESC"
    with _db() as conn:
        return [
            {"feature": r["feature"], "cost_usd": float(r["cost_usd"] or 0),
             "calls": int(r["calls"] or 0)}
            for r in conn.execute(q, params).fetchall()
        ]


def list_pending_payments_for_staff(staff_id: int, limit: int = 5) -> list[dict]:
    """
    Открытые уведомления мастера, по которым он ещё не нажал кнопку оплаты.
    Используется для голосового ответа с Apple Watch: «наличные»/«карта»
    закрывают САМУЮ СВЕЖУЮ такую запись.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_advice_log WHERE staff_id = ? "
            "AND button_pressed IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (staff_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Реактивация уснувших клиентов ────────────────────────────────────

def list_telegram_clients() -> list[dict]:
    """Все клиенты, которые когда-то взаимодействовали с ботом (есть chat_id)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM clients WHERE telegram_chat_id IS NOT NULL "
            "AND phone_enc IS NOT NULL"
        ).fetchall()
        return [_client_row_to_dict(r) for r in rows]


def was_recently_reactivated(client_id: int, days: int = 14) -> bool:
    """True, если клиенту уже слали реактивацию в последние N дней."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reactivation_log WHERE client_id = ? "
            "AND action IN ('sent','engaged') AND sent_at > ? LIMIT 1",
            (client_id, cutoff),
        ).fetchone()
        return bool(row)


def was_recently_declined(client_id: int, days: int = 30) -> bool:
    """True, если клиент недавно отказался — не беспокоим N дней."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reactivation_log WHERE client_id = ? "
            "AND action = 'declined' AND sent_at > ? LIMIT 1",
            (client_id, cutoff),
        ).fetchone()
        return bool(row)


def log_reactivation(client_id: int, action: str):
    """Фиксирует событие реактивации (sent/declined/engaged/blocked)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO reactivation_log (client_id, action, sent_at) "
            "VALUES (?, ?, ?)",
            (client_id, action, _now()),
        )


# ─── Напоминания по индивидуальному циклу ─────────────────────────

def was_recently_cycle_reminded(client_id: int, days: int = 10) -> bool:
    """True, если клиенту слали цикл-напоминание в последние N дней."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM cycle_reminder_log WHERE client_id = ? "
            "AND sent_at > ? LIMIT 1",
            (client_id, cutoff),
        ).fetchone()
        return bool(row)


def log_cycle_reminder(
    client_id: int,
    avg_cycle_days: int,
    predicted_visit: str,
    action: str = "sent",
):
    """Регистрирует событие цикл-напоминания."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO cycle_reminder_log "
            "(client_id, avg_cycle_days, predicted_visit, action, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, avg_cycle_days, predicted_visit, action, _now()),
        )


# ─── Сбор отзывов ────────────────────────────────────────────────

def schedule_review_request(
    client_id: int,
    record_id: int,
    staff_id: int | None,
    delay_hours: int = 3,
) -> bool:
    """
    Планирует запрос на отзыв через delay_hours после закрытия визита.
    Возвращает True если запланировали, False если уже было запланировано
    для этой пары (client_id, record_id) — защита от двойного запроса.
    """
    visit_closed_at = _now()
    send_after = (datetime.now() + timedelta(hours=delay_hours)).isoformat(
        timespec="seconds"
    )
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO review_requests "
                "(client_id, record_id, staff_id, visit_closed_at, send_after, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (client_id, record_id, staff_id, visit_closed_at, send_after),
            )
            return True
        except psycopg2.IntegrityError:
            return False


def pending_review_requests_to_send(now_iso: str | None = None) -> list[dict]:
    """Запросы, у которых статус 'pending' и send_after уже наступил."""
    now_iso = now_iso or _now()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM review_requests "
            "WHERE status = 'pending' AND send_after <= ? "
            "ORDER BY send_after",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_review_request_sent(review_id: int):
    with _db() as conn:
        conn.execute(
            "UPDATE review_requests SET status = 'sent', sent_at = ? WHERE id = ?",
            (_now(), review_id),
        )


def mark_review_request_failed(review_id: int, reason: str = "send_failed"):
    """Когда клиент заблокировал бот или ошибка отправки."""
    with _db() as conn:
        conn.execute(
            "UPDATE review_requests SET status = ? WHERE id = ?",
            (reason, review_id),
        )


def record_review_response(
    review_id: int,
    rating: int,
    comment: str | None = None,
):
    """Сохраняет ответ клиента на запрос отзыва."""
    with _db() as conn:
        conn.execute(
            "UPDATE review_requests SET rating = ?, comment = ?, "
            "responded_at = ?, status = 'responded' WHERE id = ?",
            (rating, comment, _now(), review_id),
        )


def get_review_request_by_id(review_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM review_requests WHERE id = ?", (review_id,)
        ).fetchone()
        return dict(row) if row else None


def expire_stale_review_requests(stale_days: int = 7) -> int:
    """Помечает 'sent', на которые клиент не ответил за N дней, как expired."""
    cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat(timespec="seconds")
    with _db() as conn:
        cur = conn.execute(
            "UPDATE review_requests SET status = 'expired' "
            "WHERE status = 'sent' AND sent_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def review_stats(days: int = 30) -> dict:
    """Сводка по отзывам за N дней (для команды /reviews_stats)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM review_requests "
            "WHERE visit_closed_at >= ? GROUP BY status",
            (cutoff,),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        # Средний рейтинг и распределение
        rating_rows = conn.execute(
            "SELECT rating, COUNT(*) AS n FROM review_requests "
            "WHERE visit_closed_at >= ? AND rating IS NOT NULL GROUP BY rating",
            (cutoff,),
        ).fetchall()
        by_rating = {r["rating"]: r["n"] for r in rating_rows}
        total_rated = sum(by_rating.values())
        avg = (
            sum(r * n for r, n in by_rating.items()) / total_rated
            if total_rated else None
        )
    return {
        "by_status": by_status,
        "by_rating": by_rating,
        "total_requested": sum(by_status.values()),
        "total_rated": total_rated,
        "avg_rating": avg,
    }


def list_recent_reviews(limit: int = 40, days: int = 180) -> list[dict]:
    """Последние отзывы с оценкой (для модерации в панели управления)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        rows = conn.execute(
            "SELECT rating, comment, staff_id, visit_closed_at, responded_at "
            "FROM review_requests WHERE rating IS NOT NULL AND visit_closed_at >= ? "
            "ORDER BY COALESCE(responded_at, visit_closed_at) DESC LIMIT ?",
            (cutoff, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Алерт админу о зависшей заявке ──────────────────────────────

def upsert_client_chat_state(
    client_id: int,
    *,
    client_message: str | None = None,
    ai_reply: str | None = None,
    resolved_reason: str | None = None,
    booking_intent: bool | None = None,
):
    """Обновляет состояние «текущего диалога» клиента.

    • client_message  — клиент прислал сообщение: обновляем last_client_*,
      сбрасываем resolved_*/alerted_at. booking_intent «прилипает» (MAX) —
      если хоть раз было намерение записаться, флаг остаётся до закрытия.
    • ai_reply        — AI ответил, фиксируем текст для контекста алерта.
    • resolved_reason — «booked»/«declined»/«alerted» закрывает эпизод и
      сбрасывает booking_intent.
    """
    now = _now()
    intent_val = 1 if booking_intent else 0
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO client_chat_state (client_id) VALUES (?)",
            (client_id,),
        )
        if client_message is not None:
            conn.execute(
                "UPDATE client_chat_state SET "
                "last_client_message_at = ?, last_client_message = ?, "
                "resolved_reason = NULL, resolved_at = NULL, alerted_at = NULL, "
                "booking_intent = GREATEST(COALESCE(booking_intent, 0), ?) "
                "WHERE client_id = ?",
                (now, (client_message or "")[:500], intent_val, client_id),
            )
        if ai_reply is not None:
            conn.execute(
                "UPDATE client_chat_state SET "
                "last_ai_reply = ?, last_ai_reply_at = ? "
                "WHERE client_id = ?",
                ((ai_reply or "")[:1000], now, client_id),
            )
        if resolved_reason is not None:
            conn.execute(
                "UPDATE client_chat_state SET "
                "resolved_reason = ?, resolved_at = ?, booking_intent = 0 "
                "WHERE client_id = ?",
                (resolved_reason, now, client_id),
            )


def find_pending_lead_alerts(
    min_idle_minutes: int = 30,
    max_idle_minutes: int = 180,
) -> list[dict]:
    """Кандидаты для алерта о зависшей заявке.

    Условия (строгие, чтобы не спамить):
      • было НАМЕРЕНИЕ записаться (booking_intent = 1)
      • молчание min_idle..max_idle минут (не «только что», но и не вчера)
      • эпизод не закрыт (resolved_at IS NULL) и ещё не алертили (alerted_at NULL)
    """
    now = datetime.now()
    idle_cutoff = (now - timedelta(minutes=min_idle_minutes)).isoformat(timespec="seconds")
    fresh_cutoff = (now - timedelta(minutes=max_idle_minutes)).isoformat(timespec="seconds")
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM client_chat_state
            WHERE booking_intent = 1
              AND last_client_message_at IS NOT NULL
              AND last_client_message_at <= ?
              AND last_client_message_at >= ?
              AND resolved_at IS NULL
              AND alerted_at IS NULL
            ORDER BY last_client_message_at
            """,
            (idle_cutoff, fresh_cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_lead_alerted(client_id: int):
    """Помечаем как алертнутый И закрываем эпизод — один алерт на попытку.
    Новое сообщение клиента откроет эпизод заново (resolved сбросится)."""
    now = _now()
    with _db() as conn:
        conn.execute(
            "UPDATE client_chat_state SET alerted_at = ?, "
            "resolved_reason = 'alerted', resolved_at = ? WHERE client_id = ?",
            (now, now, client_id),
        )


def lead_alert_stats(days: int = 30) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        total_alerts = conn.execute(
            "SELECT COUNT(*) AS n FROM client_chat_state "
            "WHERE alerted_at >= ?", (cutoff,),
        ).fetchone()["n"]
        rescued = conn.execute(
            "SELECT COUNT(*) AS n FROM client_chat_state "
            "WHERE alerted_at >= ? AND resolved_reason = 'booked' "
            "AND resolved_at > alerted_at",
            (cutoff,),
        ).fetchone()["n"]
        by_reason = {
            r["resolved_reason"]: r["n"]
            for r in conn.execute(
                "SELECT resolved_reason, COUNT(*) AS n FROM client_chat_state "
                "WHERE resolved_at >= ? AND resolved_reason IS NOT NULL "
                "GROUP BY resolved_reason",
                (cutoff,),
            ).fetchall()
        }
    return {
        "alerts_sent": total_alerts,
        "rescued_after_alert": rescued,
        "by_reason": by_reason,
    }


# ─── Предложения освободившегося слота ────────────────────────────

def was_recently_offered_freed_slot(client_id: int, days: int = 7) -> bool:
    """True, если клиенту слали оффер освободившегося слота за N дней."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM freed_slot_offers WHERE client_id = ? "
            "AND offered_at > ? AND action IN ('sent', 'accepted') LIMIT 1",
            (client_id, cutoff),
        ).fetchone()
        return bool(row)


def log_freed_slot_offer(client_id: int, staff_id: int, slot_datetime: str,
                          action: str = "sent"):
    """Регистрирует факт оффера. action: sent / blocked / accepted / declined."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO freed_slot_offers "
            "(client_id, staff_id, slot_datetime, offered_at, action) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, staff_id, slot_datetime, _now(), action),
        )


# ─── Абонементы ───────────────────────────────────────────────────

def create_subscription(*, client_id: int, plan_code: str, tier: str,
                         price_rub: int, visits_included: int,
                         started_at: str, expires_at: str) -> int:
    """Создаёт подписку со статусом pending_payment. Возвращает id."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO subscriptions "
            "(client_id, plan_code, tier, price_rub, visits_included, "
            "started_at, expires_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_payment', ?)",
            (client_id, plan_code, tier, price_rub, visits_included,
             started_at, expires_at, _now()),
        )
        return cur.lastrowid


def set_subscription_payment_id(subscription_id: int, payment_id: str):
    with _db() as conn:
        conn.execute(
            "UPDATE subscriptions SET yukassa_payment_id = ? WHERE id = ?",
            (payment_id, subscription_id),
        )


def get_subscription(subscription_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,),
        ).fetchone()
        return dict(row) if row else None


def get_subscription_by_payment_id(payment_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE yukassa_payment_id = ? LIMIT 1",
            (payment_id,),
        ).fetchone()
        return dict(row) if row else None


def activate_subscription(subscription_id: int):
    """pending_payment → active + проставляет payment_completed_at."""
    with _db() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = 'active', "
            "payment_completed_at = ? WHERE id = ?",
            (_now(), subscription_id),
        )


def update_subscription_status(subscription_id: int, status: str):
    """active / expired / refunded."""
    with _db() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = ? WHERE id = ?",
            (status, subscription_id),
        )


def update_subscription_usage(subscription_id: int, visits_used: int):
    with _db() as conn:
        conn.execute(
            "UPDATE subscriptions SET visits_used = ? WHERE id = ?",
            (visits_used, subscription_id),
        )


def mark_subscription_renew_pushed(subscription_id: int):
    with _db() as conn:
        conn.execute(
            "UPDATE subscriptions SET renew_reminder_sent_at = ? WHERE id = ?",
            (_now(), subscription_id),
        )


def get_active_subscription_for_client(client_id: int) -> dict | None:
    """Самая свежая активная подписка клиента (если есть)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE client_id = ? AND status = 'active' "
            "ORDER BY expires_at DESC LIMIT 1",
            (client_id,),
        ).fetchone()
        return dict(row) if row else None


def list_active_subscriptions() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE status = 'active' "
            "ORDER BY expires_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_subscriptions_for_client_ever(client_id: int) -> list[dict]:
    """Все когда-либо купленные подписки клиента (active + expired) —
    нужно для проверки «покрывался ли визит подпиской» в loyalty."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE client_id = ? "
            "AND status IN ('active', 'expired') "
            "ORDER BY started_at DESC",
            (client_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Программа лояльности ────────────────────────────────────────

def add_loyalty_transaction(*, client_id: int, type_: str, points: int,
                              visit_record_id: int | None = None,
                              note: str | None = None):
    """
    Добавляет одну транзакцию баланса. type: 'earn' / 'redeem' / 'expire'.
    Для 'earn' с указанным visit_record_id защита от дубля через UNIQUE-индекс.
    """
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO loyalty_transactions "
                "(client_id, type, points, visit_record_id, note, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (client_id, type_, int(points), visit_record_id, note, _now()),
            )
    except psycopg2.IntegrityError:
        # Дубликат earn по тому же visit — ок, тихо пропускаем
        pass


def loyalty_balance(client_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS bal FROM loyalty_transactions "
            "WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return int(row["bal"] or 0)


def client_has_loyalty_backfill(client_id: int) -> bool:
    """True, если уже выдавали welcome-бонус (защита от повторного начисления)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM loyalty_transactions WHERE client_id = ? "
            "AND type = 'backfill' LIMIT 1",
            (client_id,),
        ).fetchone()
        return bool(row)


def loyalty_redemption_exists(client_id: int, visit_record_id: int,
                                service_title: str) -> bool:
    """True если уже списали баллы за этот record + услугу (защита от дубля)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM loyalty_transactions WHERE client_id = ? "
            "AND visit_record_id = ? AND type = 'redeem' AND note LIKE ? LIMIT 1",
            (client_id, visit_record_id, f"%{service_title}%"),
        ).fetchone()
        return bool(row)


def loyalty_redemptions_for_record(visit_record_id: int) -> list[dict]:
    """Все redeem-транзакции, привязанные к данной YClients-записи."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM loyalty_transactions "
            "WHERE visit_record_id = ? AND type = 'redeem'",
            (visit_record_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def loyalty_refund_exists(client_id: int, visit_record_id: int) -> bool:
    """True если уже возвращали баллы по этой записи."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM loyalty_transactions WHERE client_id = ? "
            "AND visit_record_id = ? AND type = 'refund' LIMIT 1",
            (client_id, visit_record_id),
        ).fetchone()
        return bool(row)


def loyalty_already_earned_for_record(client_id: int, visit_record_id: int) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM loyalty_transactions WHERE client_id = ? "
            "AND visit_record_id = ? AND type = 'earn' LIMIT 1",
            (client_id, visit_record_id),
        ).fetchone()
        return bool(row)


def loyalty_code_is_free(code: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM loyalty_redeem_codes WHERE code = ? LIMIT 1", (code,),
        ).fetchone()
        return row is None


def create_loyalty_code(*, code: str, client_id: int, service_title: str,
                          points: int, expires_at: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO loyalty_redeem_codes "
            "(code, client_id, service_title, points, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, client_id, service_title, points, _now(), expires_at),
        )


def get_loyalty_code(code: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM loyalty_redeem_codes WHERE code = ?", (code,),
        ).fetchone()
        return dict(row) if row else None


def mark_loyalty_code_used(code: str, admin_id: int):
    with _db() as conn:
        conn.execute(
            "UPDATE loyalty_redeem_codes SET used_at = ?, used_by_admin_id = ? "
            "WHERE code = ?",
            (_now(), admin_id, code),
        )


def claim_loyalty_code(code: str, admin_id: int) -> bool:
    """Атомарно «застолбить» код за погашением: помечает used_at ТОЛЬКО если код
    ещё не погашен. Возвращает True, если именно этот вызов застолбил код.
    UPDATE ... WHERE used_at IS NULL атомарен в SQLite, поэтому два параллельных
    погашения (двойной тап кассира / два кассира) не спишут баллы дважды —
    выиграет ровно один, второй получит False."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE loyalty_redeem_codes SET used_at = ?, used_by_admin_id = ? "
            "WHERE code = ? AND used_at IS NULL",
            (_now(), admin_id, code),
        )
        return cur.rowcount > 0


def dashboard_metrics(days: int = 30, from_iso: str = None, to_iso: str = None) -> dict:
    """
    Все ключевые цифры для /dashboard за выбранный период.
    Делает один проход по нужным таблицам — экономит вызовы.

    Возвращает:
      acquisition: {total_new, by_category, top_sources}
      revenue:     {via_yukassa_rub, subscriptions_rub, gift_certs_rub}
      bookings:    {created, with_record_id}
      subscriptions: {active, new_in_period, expiring_soon}
      loyalty:     {earned_period, redeemed_period, expired_period,
                    active_clients_total}
      reviews:     {requested, responded, avg_rating, rescued_negatives}
      lead_alerts: {alerts_sent, rescued}
      ai:          {cost_usd, cost_rub}
      period:      {days, from_iso, to_iso}
    """
    # Период можно задать явно (from_iso .. to_iso, верхняя граница ИСКЛЮЧИТЕЛЬНАЯ —
    # т.е. < to_iso). Это нужно для календарных месяцев: у «прошлого месяца»
    # обязательна верхняя граница, иначе захватит и текущий. Если даты не заданы —
    # последние `days` дней до сейчас (обратная совместимость).
    now_iso = _now()
    if from_iso is None:
        from_iso = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    if to_iso is None:
        to_iso = now_iso

    with _db() as conn:
        # ── Привлечение ────────────────────────────────────────────
        # Новые клиенты — те, у кого first_source_at в окне
        new_clients_rows = conn.execute(
            "SELECT first_source AS src, COUNT(*) AS n "
            "FROM clients WHERE first_source_at >= ? AND first_source_at < ? "
            "GROUP BY first_source",
            (from_iso, to_iso),
        ).fetchall()
        total_new = sum(r["n"] for r in new_clients_rows)
        by_source = {(r["src"] or "unknown"): int(r["n"]) for r in new_clients_rows}

        # ── Записи через бот ───────────────────────────────────────
        bookings_created = conn.execute(
            "SELECT COUNT(*) AS n FROM bookings WHERE created_at >= ? AND created_at < ?",
            (from_iso, to_iso),
        ).fetchone()["n"]
        bookings_with_record = conn.execute(
            "SELECT COUNT(*) AS n FROM bookings "
            "WHERE created_at >= ? AND created_at < ? AND yclients_record_id IS NOT NULL",
            (from_iso, to_iso),
        ).fetchone()["n"]

        # ── Абонементы ─────────────────────────────────────────────
        subs_active = conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE status = 'active'"
        ).fetchone()["n"]
        subs_new = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(price_rub), 0) AS revenue "
            "FROM subscriptions WHERE created_at >= ? AND created_at < ? AND payment_completed_at IS NOT NULL",
            (from_iso, to_iso),
        ).fetchone()
        subs_revenue_period = int(subs_new["revenue"] or 0)
        subs_new_count = int(subs_new["n"] or 0)
        # Истекают на следующей неделе
        week_ahead = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
        subs_expiring_soon = conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions "
            "WHERE status = 'active' AND expires_at BETWEEN ? AND ?",
            (now_iso, week_ahead),
        ).fetchone()["n"]

        # ── Сертификаты ─────────────────────────────────────────────
        cert_rev = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS n "
            "FROM gift_certificates WHERE issued_at >= ? AND issued_at < ? AND payment_status = 'paid'",
            (from_iso, to_iso),
        ).fetchone()
        cert_revenue_period = int(cert_rev["s"] or 0)
        cert_count_period = int(cert_rev["n"] or 0)
        # Активные сертификаты: оплачены, ещё НЕ погашены и срок НЕ истёк
        # (не зависит от периода — это «живые» сертификаты на руках у клиентов).
        cert_active = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS n "
            "FROM gift_certificates "
            "WHERE payment_status = 'paid' AND used_at IS NULL AND expires_at > ?",
            (now_iso,),
        ).fetchone()
        cert_active_value = int(cert_active["s"] or 0)
        cert_active_count = int(cert_active["n"] or 0)

        # ── Лояльность (за период) ──────────────────────────────────
        # В loyalty_transactions поле времени называется `at`, не created_at.
        earned_period = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS s FROM loyalty_transactions "
            "WHERE type = 'earn' AND at >= ? AND at < ?",
            (from_iso, to_iso),
        ).fetchone()["s"]
        redeemed_period = conn.execute(
            "SELECT COALESCE(SUM(-points), 0) AS s FROM loyalty_transactions "
            "WHERE type = 'redeem' AND at >= ? AND at < ?",
            (from_iso, to_iso),
        ).fetchone()["s"]
        expired_period = conn.execute(
            "SELECT COALESCE(SUM(-points), 0) AS s FROM loyalty_transactions "
            "WHERE type = 'expire' AND at >= ? AND at < ?",
            (from_iso, to_iso),
        ).fetchone()["s"]
        loyalty_active_total = conn.execute(
            "SELECT COUNT(DISTINCT client_id) AS c FROM loyalty_transactions"
        ).fetchone()["c"]

        # ── Отзывы (за период) ──────────────────────────────────────
        reviews_requested = conn.execute(
            "SELECT COUNT(*) AS n FROM review_requests WHERE visit_closed_at >= ? AND visit_closed_at < ?",
            (from_iso, to_iso),
        ).fetchone()["n"]
        reviews_responded = conn.execute(
            "SELECT COUNT(*) AS n FROM review_requests "
            "WHERE visit_closed_at >= ? AND visit_closed_at < ? AND rating IS NOT NULL",
            (from_iso, to_iso),
        ).fetchone()["n"]
        avg_row = conn.execute(
            "SELECT AVG(rating) AS a FROM review_requests "
            "WHERE visit_closed_at >= ? AND visit_closed_at < ? AND rating IS NOT NULL",
            (from_iso, to_iso),
        ).fetchone()
        avg_rating = float(avg_row["a"]) if avg_row["a"] is not None else None
        # «Спасённые» негативы = ответы с rating <= 3 и комментарием
        rescued_negatives = conn.execute(
            "SELECT COUNT(*) AS n FROM review_requests "
            "WHERE visit_closed_at >= ? AND visit_closed_at < ? AND rating IS NOT NULL "
            "AND rating <= 3 AND comment IS NOT NULL AND TRIM(comment) != ''",
            (from_iso, to_iso),
        ).fetchone()["n"]

        # ── Lead-alerts ─────────────────────────────────────────────
        alerts_sent = conn.execute(
            "SELECT COUNT(*) AS n FROM client_chat_state WHERE alerted_at >= ? AND alerted_at < ?",
            (from_iso, to_iso),
        ).fetchone()["n"]
        rescued_after_alert = conn.execute(
            "SELECT COUNT(*) AS n FROM client_chat_state "
            "WHERE alerted_at >= ? AND alerted_at < ? AND resolved_reason = 'booked' "
            "AND resolved_at > alerted_at",
            (from_iso, to_iso),
        ).fetchone()["n"]

        # ── AI-расход (за период) ───────────────────────────────────
        ai_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n "
            "FROM ai_usage_log WHERE at >= ? AND at < ?",
            (from_iso, to_iso),
        ).fetchone()
        ai_cost_usd = float(ai_row["s"] or 0)
        ai_calls = int(ai_row["n"] or 0)

    # Конвертация USD → ₽ (грубо 100 ₽/$, не критично, чисто для контекста)
    ai_cost_rub = round(ai_cost_usd * 100)

    return {
        "period": {
            "days": days,
            "from_iso": from_iso,
            "to_iso": to_iso,
        },
        "acquisition": {
            "total_new": int(total_new),
            "by_source": by_source,
        },
        "bookings": {
            "created": int(bookings_created),
            "with_record_id": int(bookings_with_record),
        },
        "subscriptions": {
            "active": int(subs_active),
            "new_in_period": subs_new_count,
            "new_revenue_rub": subs_revenue_period,
            "expiring_soon": int(subs_expiring_soon),
        },
        "gift_certs": {
            "count": cert_count_period,
            "revenue_rub": cert_revenue_period,
            "active_count": cert_active_count,
            "active_value_rub": cert_active_value,
        },
        "loyalty": {
            "earned": int(earned_period or 0),
            "redeemed": int(redeemed_period or 0),
            "expired": int(expired_period or 0),
            "active_total": int(loyalty_active_total or 0),
        },
        "reviews": {
            "requested": int(reviews_requested),
            "responded": int(reviews_responded),
            "avg_rating": avg_rating,
            "rescued_negatives": int(rescued_negatives),
        },
        "lead_alerts": {
            "alerts_sent": int(alerts_sent),
            "rescued": int(rescued_after_alert),
        },
        "ai": {
            "cost_usd": ai_cost_usd,
            "cost_rub": ai_cost_rub,
            "calls": ai_calls,
        },
    }


def dashboard_timeseries(days: int = 30, start: str = None, end: str = None) -> dict:
    """Поденная динамика за период: новые клиенты, записи, выручка (₽ из абонементов+сертификатов).
    Можно задать явный диапазон дат start..end (YYYY-MM-DD) — для календарных месяцев."""
    if start and end:
        try:
            sd = date.fromisoformat(start); ed = date.fromisoformat(end)
        except Exception:
            sd = date.today() - timedelta(days=29); ed = date.today()
        n = max(1, min((ed - sd).days + 1, 400))
        start_day = sd
    else:
        n = max(1, min(int(days), 120))
        start_day = date.today() - timedelta(days=n - 1)
    from_iso = datetime.combine(start_day, datetime.min.time()).isoformat(timespec="seconds")
    labels = [(start_day + timedelta(days=i)).isoformat() for i in range(n)]
    idx = {d: i for i, d in enumerate(labels)}
    new_clients = [0] * n
    bookings = [0] * n
    revenue = [0] * n
    with _db() as conn:
        for r in conn.execute(
            "SELECT substr(first_source_at,1,10) AS d, COUNT(*) AS n FROM clients "
            "WHERE first_source_at >= ? GROUP BY d", (from_iso,)):
            if r["d"] in idx:
                new_clients[idx[r["d"]]] = int(r["n"])
        for r in conn.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM bookings "
            "WHERE created_at >= ? GROUP BY d", (from_iso,)):
            if r["d"] in idx:
                bookings[idx[r["d"]]] = int(r["n"])
        for r in conn.execute(
            "SELECT substr(created_at,1,10) AS d, COALESCE(SUM(price_rub),0) AS s FROM subscriptions "
            "WHERE created_at >= ? AND payment_completed_at IS NOT NULL GROUP BY d", (from_iso,)):
            if r["d"] in idx:
                revenue[idx[r["d"]]] += int(r["s"] or 0)
        for r in conn.execute(
            "SELECT substr(issued_at,1,10) AS d, COALESCE(SUM(amount),0) AS s FROM gift_certificates "
            "WHERE issued_at >= ? AND payment_status = 'paid' GROUP BY d", (from_iso,)):
            if r["d"] in idx:
                revenue[idx[r["d"]]] += int(r["s"] or 0)
    return {"labels": labels, "new_clients": new_clients, "bookings": bookings, "revenue": revenue}


def reviews_by_master(days: int = 90) -> dict:
    """Средний рейтинг и число оценок по каждому мастеру (staff_id) за период."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out = {}
    with _db() as conn:
        for r in conn.execute(
            "SELECT staff_id, AVG(rating) AS a, COUNT(*) AS n FROM review_requests "
            "WHERE rating IS NOT NULL AND visit_closed_at >= ? GROUP BY staff_id", (cutoff,)):
            out[r["staff_id"]] = {"avg": float(r["a"]) if r["a"] is not None else None, "n": int(r["n"])}
    return out


def loyalty_summary() -> dict:
    """Свод для /loyalty_stats: всего баллов в обороте, активных клиентов, кодов и т.п."""
    with _db() as conn:
        total_earned = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS s FROM loyalty_transactions WHERE type='earn'"
        ).fetchone()["s"]
        total_redeemed = conn.execute(
            "SELECT COALESCE(SUM(-points), 0) AS s FROM loyalty_transactions WHERE type='redeem'"
        ).fetchone()["s"]
        total_expired = conn.execute(
            "SELECT COALESCE(SUM(-points), 0) AS s FROM loyalty_transactions WHERE type='expire'"
        ).fetchone()["s"]
        active_clients = conn.execute(
            "SELECT COUNT(DISTINCT client_id) AS c FROM loyalty_transactions"
        ).fetchone()["c"]
        outstanding = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS s FROM loyalty_transactions"
        ).fetchone()["s"]
        codes_issued = conn.execute(
            "SELECT COUNT(*) AS c FROM loyalty_redeem_codes"
        ).fetchone()["c"]
        codes_used = conn.execute(
            "SELECT COUNT(*) AS c FROM loyalty_redeem_codes WHERE used_at IS NOT NULL"
        ).fetchone()["c"]
        return {
            "earned": int(total_earned or 0),
            "redeemed": int(total_redeemed or 0),
            "expired": int(total_expired or 0),
            "outstanding": int(outstanding or 0),
            "active_clients": int(active_clients or 0),
            "codes_issued": int(codes_issued or 0),
            "codes_used": int(codes_used or 0),
        }


def subscriptions_summary() -> dict:
    """Сводка по всем подпискам — для админа /subscriptions_stats."""
    with _db() as conn:
        by_status = {}
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM subscriptions GROUP BY status"
        ).fetchall():
            by_status[r["status"]] = int(r["c"])
        revenue = conn.execute(
            "SELECT COALESCE(SUM(price_rub), 0) AS total FROM subscriptions "
            "WHERE status IN ('active', 'expired')"
        ).fetchone()["total"]
        active_by_plan = {}
        for r in conn.execute(
            "SELECT plan_code, COUNT(*) AS c FROM subscriptions "
            "WHERE status = 'active' GROUP BY plan_code"
        ).fetchall():
            active_by_plan[r["plan_code"]] = int(r["c"])
        return {
            "by_status": by_status,
            "active_by_plan": active_by_plan,
            "revenue_rub": int(revenue),
        }


# ─── Реферальная программа ────────────────────────────────────────

def get_referral_code(client_id: int) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return row["code"] if row else None


def is_ref_code_free(code: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM referral_codes WHERE code = ? LIMIT 1", (code,),
        ).fetchone()
        return row is None


def create_ref_code(client_id: int, code: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO referral_codes (client_id, code, created_at) "
            "VALUES (?, ?, ?)",
            (client_id, code, _now()),
        )


def get_client_by_ref_code(code: str) -> dict | None:
    """Возвращает клиента-реферера по его коду (с расшифрованным именем)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT c.* FROM clients c "
            "JOIN referral_codes r ON r.client_id = c.id "
            "WHERE r.code = ? LIMIT 1",
            (code,),
        ).fetchone()
        return _client_row_to_dict(row) if row else None


def get_referral_for_referee(referee_chat_id: int) -> dict | None:
    """Возвращает первую (любую активную) привязку для приведённого друга."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM referrals WHERE referee_chat_id = ? "
            "ORDER BY id ASC LIMIT 1",
            (referee_chat_id,),
        ).fetchone()
        return dict(row) if row else None


def create_referral(referrer_client_id: int, referee_chat_id: int, code_used: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO referrals "
            "(referrer_client_id, referee_chat_id, code_used, joined_at, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (referrer_client_id, referee_chat_id, code_used, _now()),
        )


def list_pending_referrals() -> list[dict]:
    """Все рефералы в статусе pending — для ежедневного резолвера."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM referrals WHERE status = 'pending' "
            "ORDER BY joined_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def set_referral_referee_client(referral_id: int, referee_client_id: int):
    with _db() as conn:
        conn.execute(
            "UPDATE referrals SET referee_client_id = ? WHERE id = ?",
            (referee_client_id, referral_id),
        )


def update_referral_status(referral_id: int, status: str):
    """status: pending / granted / self_block / expired."""
    with _db() as conn:
        conn.execute(
            "UPDATE referrals SET status = ? WHERE id = ?",
            (status, referral_id),
        )


def save_referral_promo(*, referral_id: int, client_id: int, code: str,
                         kind: str, percent: int, expires_at: str):
    """kind: referrer (тому, кто пригласил) / referee (приведённому)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO referral_promos "
            "(referral_id, client_id, code, kind, percent, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (referral_id, client_id, code, kind, percent, _now(), expires_at),
        )


def referral_stats_for_client(client_id: int) -> dict:
    """Сколько друзей этот клиент уже привёл (granted) и сколько ещё в pending."""
    with _db() as conn:
        granted = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals "
            "WHERE referrer_client_id = ? AND status = 'granted'",
            (client_id,),
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals "
            "WHERE referrer_client_id = ? AND status = 'pending'",
            (client_id,),
        ).fetchone()["c"]
        return {"granted": int(granted), "pending": int(pending)}


def referral_summary() -> dict:
    """Общая статистика для админа: всего и по статусам."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM referrals GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: int(r["c"]) for r in rows}
        codes_total = conn.execute(
            "SELECT COUNT(*) AS c FROM referral_codes"
        ).fetchone()["c"]
        promos_total = conn.execute(
            "SELECT COUNT(*) AS c FROM referral_promos"
        ).fetchone()["c"]
        return {
            "by_status": by_status,
            "codes_issued": int(codes_total),
            "promos_issued": int(promos_total),
        }


# ─── Промокоды на день рождения ────────────────────────────────────

def already_sent_birthday_this_year(client_id: int, year: int) -> bool:
    """Защита от двойной отправки — один промокод в году."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM birthday_promo WHERE client_id = ? AND year = ? LIMIT 1",
            (client_id, year),
        ).fetchone()
        return bool(row)


def new_birthday_promo_code() -> str:
    """Уникальный код вида BDAY-XXXXXX (без 0/O/1/I/L)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "BDAY-" + "".join(secrets.choice(alphabet) for _ in range(6))


def save_birthday_promo(
    client_id: int, code: str, percent: int, year: int, expires_at: str
):
    """Регистрирует промокод."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO birthday_promo "
            "(client_id, code, percent, year, sent_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, code, percent, year, _now(), expires_at),
        )


def get_active_birthday_promo(telegram_chat_id: int) -> dict | None:
    """
    Возвращает действующий (не использованный, не просроченный) ДР-промокод
    клиента — нужен AI при записи, чтобы напомнить.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT bp.* FROM birthday_promo bp "
            "JOIN clients c ON c.id = bp.client_id "
            "WHERE c.telegram_chat_id = ? AND bp.used_at IS NULL "
            "AND bp.expires_at > ? "
            "ORDER BY bp.id DESC LIMIT 1",
            (telegram_chat_id, _now()),
        ).fetchone()
        return dict(row) if row else None


def mark_birthday_promo_used(code: str) -> bool:
    """Помечает промокод использованным."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE birthday_promo SET used_at = ? "
            "WHERE code = ? AND used_at IS NULL",
            (_now(), code),
        )
        return cur.rowcount > 0


# ─── Ротация ПД (152-ФЗ: не хранить дольше нужного) ──────────────────

def rotate_old_pii(retention_months: int) -> dict:
    """
    Обезличивает клиентов без активности N месяцев и сертификаты, истёкшие
    больше N месяцев назад. Фактически зануляет name_enc/phone_enc/phone_hash,
    но саму строку оставляет — чтобы не сломались связи с bookings.

    Возвращает счётчики {"clients": N, "gift_certs": M} для лога.
    """
    cutoff = (datetime.now() - timedelta(days=retention_months * 30)).isoformat(
        timespec="seconds"
    )
    counts = {"clients": 0, "gift_certs": 0}

    with _db() as conn:
        # Клиенты: нет ни одной записи позже cutoff. Через GROUP BY + HAVING.
        # Дополнительно условие на updated_at — чтобы только что зарегистрированных
        # без записей не обезличивать (вдруг человек ввёл данные но не успел дойти
        # до выбора времени).
        rows = conn.execute(
            """
            SELECT c.id FROM clients c
            LEFT JOIN bookings b ON b.client_id = c.id
            WHERE (c.name_enc IS NOT NULL OR c.phone_enc IS NOT NULL)
              AND (c.updated_at IS NULL OR c.updated_at < ?)
            GROUP BY c.id
            HAVING (MAX(b.datetime) IS NULL OR MAX(b.datetime) < ?)
            """,
            (cutoff, cutoff),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE clients SET name_enc = NULL, phone_enc = NULL, "
                "phone_hash = NULL, updated_at = ? WHERE id = ?",
                (_now(), r["id"]),
            )
            counts["clients"] += 1

        # Сертификаты: истекли давно. Хранить ПД получателя дальше нет смысла.
        rows = conn.execute(
            """
            SELECT id FROM gift_certificates
            WHERE expires_at < ?
              AND (recipient_name_enc IS NOT NULL OR recipient_phone_enc IS NOT NULL)
            """,
            (cutoff,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE gift_certificates SET recipient_name_enc = NULL, "
                "recipient_phone_enc = NULL, recipient_phone_hash = NULL "
                "WHERE id = ?",
                (r["id"],),
            )
            counts["gift_certs"] += 1

    return counts


# Создаём таблицы при импорте модуля — БД всегда готова к работе.
# init_db() отключён для Postgres (схема в 01_schema_postgres.sql)
# ─── Чаевые (аналитика по каждому мастеру) ──────────────────────────────
def save_tip(master_id=None, master_slug: str = "", master_name: str = "",
             amount=0, record_id=None) -> None:
    """Записать факт перевода чаевых (служебный сигнал клиента «Я перевёл»)."""
    try:
        amount_i = int(float(amount or 0))
    except (TypeError, ValueError):
        amount_i = 0
    with _db() as conn:
        conn.execute(
            "INSERT INTO tips (master_id, master_slug, master_name, amount, record_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(master_id) if master_id else None, master_slug or "", master_name or "",
             amount_i, int(record_id) if record_id else None, _now()),
        )


def tips_totals_by_master(from_iso: str = None, to_iso: str = None) -> list:
    """Сумма и количество чаевых по каждому мастеру за период (для владельца)."""
    q = ("SELECT master_id, MAX(master_name) AS master_name, MAX(master_slug) AS master_slug, "
         "COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total FROM tips WHERE 1=1")
    params = []
    if from_iso:
        q += " AND created_at >= ?"; params.append(from_iso)
    if to_iso:
        q += " AND created_at <= ?"; params.append(to_iso)
    q += " GROUP BY master_id ORDER BY total DESC"
    with _db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def tips_for_master(master_id, from_iso: str = None, to_iso: str = None) -> dict:
    """Сумма и количество чаевых конкретного мастера (для его собственной панели)."""
    q = "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total FROM tips WHERE master_id = ?"
    params = [int(master_id) if master_id else 0]
    if from_iso:
        q += " AND created_at >= ?"; params.append(from_iso)
    if to_iso:
        q += " AND created_at <= ?"; params.append(to_iso)
    with _db() as conn:
        r = conn.execute(q, params).fetchone()
        return {"count": (r["cnt"] or 0), "total": (r["total"] or 0)}


# ── CutMatch: лимит ИИ-консультаций (2/день на пользователя) ─────────────────
def cutmatch_count_today(user_id: int) -> int:
    """Сколько ИИ-консультаций CutMatch пользователь сделал сегодня."""
    day = datetime.now().strftime("%Y-%m-%d")
    with _db() as conn:
        row = conn.execute(
            "SELECT count FROM cutmatch_usage WHERE user_id = ? AND day = ?",
            (int(user_id), day),
        ).fetchone()
        return int(row["count"]) if row else 0


def cutmatch_incr_today(user_id: int) -> int:
    """Увеличивает дневной счётчик консультаций. Возвращает новое значение."""
    day = datetime.now().strftime("%Y-%m-%d")
    with _db() as conn:
        conn.execute(
            "INSERT INTO cutmatch_usage (user_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT (tenant_id, user_id, day) DO UPDATE SET count = cutmatch_usage.count + 1",
            (int(user_id), day),
        )
        row = conn.execute(
            "SELECT count FROM cutmatch_usage WHERE user_id = ? AND day = ?",
            (int(user_id), day),
        ).fetchone()
        return int(row["count"]) if row else 1


# ─── Аудит инструментов LLM (RBAC / risk-tiering) ────────────────────────────

def log_tool_call(user_id, role: str, tool: str, risk: str,
                  allowed: bool, reason: str = "") -> None:
    """Пишет факт вызова инструмента (разрешён/отказан). Никогда не падает —
    аудит не должен ломать основной поток. ПД сюда не попадают."""
    try:
        uid = int(user_id) if user_id else None
    except Exception:
        uid = None
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO tool_audit (ts, user_id, role, tool, risk, allowed, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), uid, role, tool, risk, 1 if allowed else 0, (reason or "")[:200]),
            )
    except Exception:
        pass


def recent_tool_audit(limit: int = 50, only_denied: bool = False) -> list:
    """Последние записи аудита (для GOD-режима «Здоровье» / дебага)."""
    try:
        with _db() as conn:
            sql = ("SELECT ts, user_id, role, tool, risk, allowed, reason FROM tool_audit "
                   + ("WHERE allowed = 0 " if only_denied else "")
                   + "ORDER BY id DESC LIMIT ?")
            return [dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]
    except Exception:
        return []


# ─── Durable-идемпотентность оплаты визита ───────────────────────────────────

def payment_already_done(record_id) -> dict | None:
    """Если оплату по этому record_id уже фиксировали — вернёт {method, amount, ts}."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT method, amount, ts FROM payment_idempotency WHERE record_id = ?",
                (int(record_id),),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def mark_payment_done(record_id, method: str, amount=None) -> None:
    """Фиксирует оплату record_id. INSERT OR IGNORE — повтор не перезатирает первую."""
    try:
        amt = int(amount) if amount else None
    except Exception:
        amt = None
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO payment_idempotency (record_id, method, amount, ts) "
                "VALUES (?, ?, ?, ?)",
                (int(record_id), method, amt, _now()),
            )
    except Exception:
        pass


# ─── Procedural-память: операционные правила салона ──────────────────────────

def add_salon_rule(rule_text: str, created_by=None) -> int:
    """Сохраняет правило салона (заданное владельцем). Возвращает id."""
    try:
        uid = int(created_by) if created_by else None
    except Exception:
        uid = None
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO salon_rules (rule_text, created_by, created_at, active) "
            "VALUES (?, ?, ?, 1)",
            ((rule_text or "").strip()[:500], uid, _now()),
        )
        return int(cur.lastrowid)


def list_salon_rules(active_only: bool = True, limit: int = 40) -> list:
    """Активные правила салона (для системного промпта MAYA / показа владельцу)."""
    try:
        with _db() as conn:
            sql = ("SELECT id, rule_text, created_at FROM salon_rules "
                   + ("WHERE active = 1 " if active_only else "")
                   + "ORDER BY id ASC LIMIT ?")
            return [dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]
    except Exception:
        return []


def deactivate_salon_rule(rule_id) -> bool:
    """Деактивирует (мягко удаляет) правило по id. True — если что-то изменилось."""
    try:
        with _db() as conn:
            cur = conn.execute(
                "UPDATE salon_rules SET active = 0 WHERE id = ? AND active = 1",
                (int(rule_id),),
            )
            return cur.rowcount > 0
    except Exception:
        return False
