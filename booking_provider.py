"""
SaaS Этап 0 — абстракция «платформа записи» (BookingProvider).

ЗАЧЕМ: сейчас весь код ходит прямо в yclients.py. Чтобы потом подключать
Altegio/Dikidi/др., вводим единый интерфейс. yclients.py становится ОДНОЙ из
реализаций (YClientsProvider). Код бота/приложения зовёт провайдера тенанта —
и не знает, какая платформа под капотом.

СТРАТЕГИЯ ВНЕДРЕНИЯ (чтобы не переписывать всё разом):
  Шаг 1. YClientsProvider просто оборачивает существующий YClientsAPI и
         возвращает ТЕ ЖЕ структуры, что код ждёт сегодня (raw YClients dict).
         Меняем только точку получения клиента: вместо глобального yclients
         → get_provider(tenant). Поведение 1:1, риск минимальный.
  Шаг 2. Постепенно нормализуем возвраты к общим dataclass-ам (ниже Normalized*),
         по одному вызову за раз. Тогда второй провайдер добавляется адаптером
         без правок в вызывающем коде.

Креды берём из tenant.provider_config (расшифрованного). Платформенные секреты
(наш partner-токен, базовый URL) — из окружения сервиса, НЕ из тенанта.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────
# Возможности провайдера: не все платформы умеют всё. Фронт/бот по этим
# флагам прячут фичи, которых у конкретной платформы нет.
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProviderCapabilities:
    finance: bool = False          # касса/транзакции, set_record_paid, удаление оплат
    tips: bool = False             # чаевые мастерам
    goods: bool = False            # товарный каталог
    loyalty_redeem: bool = False   # списание баллов в записи
    client_search: bool = False    # поиск клиентов по части номера/имени
    edit_services: bool = False    # менять состав услуг в записи


# ─────────────────────────────────────────────────────────────────────
# (Шаг 2) Нормализованные формы. Пока опциональны — вводим постепенно.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class NormalizedRecord:
    record_id: int
    datetime: str
    services: list[dict] = field(default_factory=list)  # [{id,title,cost,duration}]
    master: str | None = None
    master_id: int | None = None
    client_name: str | None = None
    client_phone: str | None = None
    attendance: int = 0
    paid_full: int = 0
    raw: dict | None = None                              # исходный объект платформы


class BookingProvider(ABC):
    """Контракт платформы записи. Реализация конструируется на креды ОДНОГО тенанта."""

    provider_name: str = "base"

    def __init__(self, tenant_id: int, config: dict):
        self.tenant_id = tenant_id
        self.config = config            # расшифрованный provider_config тенанта

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    # ── Каталог ───────────────────────────────────────────────────────
    @abstractmethod
    def get_services(self, staff_id: int | None = None) -> list[dict]: ...
    @abstractmethod
    def get_masters(self) -> list[dict]: ...
    def get_goods_catalog(self) -> list[dict]: return []   # опц. (capabilities.goods)

    # ── Расписание / слоты ────────────────────────────────────────────
    @abstractmethod
    def who_works_on(self, date_str: str) -> dict: ...
    @abstractmethod
    def get_working_masters(self, date_str: str) -> list[dict]: ...
    @abstractmethod
    def get_master_schedule(self, staff_id: int, days_ahead: int = 14) -> list[dict]: ...
    @abstractmethod
    def get_available_slots(self, staff_id: int, date: str,
                            service_ids: list[int] | None = None) -> list[dict]: ...
    @abstractmethod
    def find_nearest_slots(self, staff_id: int, service_ids: list[int] | None = None,
                           days_ahead: int = 7) -> list[dict]: ...

    # ── Записи: запись ────────────────────────────────────────────────
    @abstractmethod
    def create_record(self, staff_id: int, service_ids: list[int], datetime_str: str,
                       client_name: str | None = None, client_phone: str | None = None,
                       comment: str = "") -> dict: ...
    @abstractmethod
    def reschedule_booking(self, record_id: int, datetime_str: str,
                           staff_id: int | None = None) -> dict: ...
    @abstractmethod
    def cancel_booking(self, record_id: int) -> dict: ...
    @abstractmethod
    def set_record_attendance(self, record_id: int, attendance: int) -> dict: ...
    @abstractmethod
    def set_record_services(self, record_id: int, service_ids: list[int]) -> dict: ...
    @abstractmethod
    def set_record_client_name(self, record_id: int, client_name: str,
                               client_phone: str | None = None) -> dict: ...

    # ── Записи: чтение ────────────────────────────────────────────────
    @abstractmethod
    def get_record(self, record_id: int) -> dict | None: ...
    @abstractmethod
    def get_records_for_master(self, staff_id: int, date: str) -> list[dict]: ...
    @abstractmethod
    def get_company_records(self, start_date: str, end_date: str) -> list[dict]: ...
    @abstractmethod
    def get_client_bookings(self, phone: str) -> list[dict]: ...
    @abstractmethod
    def get_client_history(self, client_id: int, count: int = 30) -> list[dict]: ...

    # ── Клиенты ───────────────────────────────────────────────────────
    @abstractmethod
    def get_client(self, client_id: int) -> dict | None: ...
    def search_clients(self, query: str, limit: int = 8) -> list[dict]: return []   # опц.
    def list_all_clients(self, page_size: int = 200) -> list[dict]: return []       # опц.

    # ── Финансы (опционально, capabilities.finance/tips) ──────────────
    def get_company_transactions(self, start_date: str, end_date: str) -> list[dict]: return []
    def create_finance_transaction(self, *a, **kw) -> dict: raise NotImplementedError
    def delete_finance_transaction(self, transaction_id: int) -> dict: raise NotImplementedError
    def set_record_paid(self, *a, **kw) -> dict: raise NotImplementedError
    def tips_by_master(self, from_iso: str | None = None, to_iso: str | None = None) -> list[dict]: return []
    def mark_record_loyalty_redemption(self, *a, **kw) -> dict: raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────
# Реализация для YClients — обёртка вокруг существующего yclients.YClientsAPI.
# (Шаг 1: возвращаем raw-структуры как сейчас; нормализацию добавим позже.)
# ─────────────────────────────────────────────────────────────────────
class YClientsProvider(BookingProvider):
    provider_name = "yclients"

    def __init__(self, tenant_id: int, config: dict):
        super().__init__(tenant_id, config)
        # YClientsAPI станет принимать company_id/user_token/account-ids из config,
        # а partner-токен/base_url — из окружения платформы (как сейчас глобально).
        from yclients import YClientsAPI
        self.api = YClientsAPI(
            company_id=config["company_id"],
            user_token=config["user_token"],
            cash_account_id=config.get("cash_account_id"),
            cashless_account_id=config.get("cashless_account_id"),
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            finance=True, tips=True, goods=True, loyalty_redeem=True,
            client_search=True, edit_services=True,
        )

    # делегируем в self.api — имена методов уже совпадают
    def get_services(self, staff_id=None):        return self.api.get_services(staff_id)
    def get_masters(self):                          return self.api.get_masters()
    def get_goods_catalog(self):                    return self.api.get_goods_catalog()
    def who_works_on(self, date_str):               return self.api.who_works_on(date_str)
    def get_working_masters(self, date_str):        return self.api.get_working_masters(date_str)
    def get_master_schedule(self, staff_id, days_ahead=14): return self.api.get_master_schedule(staff_id, days_ahead)
    def get_available_slots(self, staff_id, date, service_ids=None): return self.api.get_available_slots(staff_id, date, service_ids)
    def find_nearest_slots(self, staff_id, service_ids=None, days_ahead=7): return self.api.find_nearest_slots(staff_id, service_ids, days_ahead)
    def create_record(self, staff_id, service_ids, datetime_str, client_name=None, client_phone=None, comment=""):
        return self.api.create_record_admin(staff_id, service_ids, datetime_str, client_name, client_phone, comment)
    def reschedule_booking(self, record_id, datetime_str, staff_id=None): return self.api.reschedule_booking(record_id, datetime_str, staff_id)
    def cancel_booking(self, record_id):            return self.api.cancel_booking(record_id)
    def set_record_attendance(self, record_id, attendance): return self.api.set_record_attendance(record_id, attendance)
    def set_record_services(self, record_id, service_ids):  return self.api.set_record_services(record_id, service_ids)
    def set_record_client_name(self, record_id, client_name, client_phone=None): return self.api.set_record_client_name(record_id, client_name, client_phone)
    def get_record(self, record_id):                return self.api.get_record(record_id)
    def get_records_for_master(self, staff_id, date): return self.api.get_records_for_master(staff_id, date)
    def get_company_records(self, start_date, end_date): return self.api.get_company_records(start_date, end_date)
    def get_client_bookings(self, phone):           return self.api.get_client_bookings(phone)
    def get_client_history(self, client_id, count=30): return self.api.get_client_history(client_id, count)
    def get_client(self, client_id):                return self.api.get_client(client_id)
    def search_clients(self, query, limit=8):       return self.api.search_clients(query, limit)
    def list_all_clients(self, page_size=200):      return self.api.list_all_clients(page_size)
    def get_company_transactions(self, start_date, end_date): return self.api.get_company_transactions(start_date, end_date)
    def create_finance_transaction(self, *a, **kw): return self.api.create_finance_transaction(*a, **kw)
    def set_record_paid(self, *a, **kw):            return self.api.set_record_paid(*a, **kw)
    def tips_by_master(self, from_iso=None, to_iso=None): return self.api.tips_by_master(from_iso, to_iso)
    def mark_record_loyalty_redemption(self, *a, **kw): return self.api.mark_record_loyalty_redemption(*a, **kw)


# ─────────────────────────────────────────────────────────────────────
# Фабрика: по строке тенанта вернуть нужного провайдера.
# ─────────────────────────────────────────────────────────────────────
_PROVIDERS = {"yclients": YClientsProvider}
# будущее: _PROVIDERS["altegio"] = AltegioProvider, ...


def get_provider(tenant: dict) -> BookingProvider:
    """
    tenant — строка из таблицы tenants (provider + расшифрованный provider_config).
    Кэшировать инстанс на тенанта стоит (TTL), чтобы не плодить http-сессии.
    """
    kind = tenant["provider"]
    cls = _PROVIDERS.get(kind)
    if not cls:
        raise ValueError(f"Неизвестная платформа записи: {kind}")
    return cls(tenant_id=tenant["id"], config=tenant["provider_config"])
