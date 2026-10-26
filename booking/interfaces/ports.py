"""Ports to the contexts booking orchestrates (LLD §8.3).

Booking's own request and result types, so the domain and services never see another context's DTOs; the
adapters in ``booking/adapters/`` translate. ``method_details`` holds raw card, UPI or wallet data: it is
excluded from repr and never logged.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money

from booking.domain.value_objects import BookingPrice, RoomTypeInfo

HoldReleaseReason = Literal["CANCELLED", "EXPIRED"]


class CatalogPort(ABC):
    @abstractmethod
    def room_type_info(self, room_type_id: UUID) -> RoomTypeInfo:
        """The room type with its property's commercial terms; NotFound when it does not exist."""


class PricingPort(ABC):
    @abstractmethod
    def quote(self, room_type: RoomTypeInfo, stay: DateRange, rooms: int) -> BookingPrice: ...


class InventoryPort(ABC):
    @abstractmethod
    def hold(
        self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> UUID:
        """Reserve ``rooms`` for every night of ``stay`` and return the hold id; Conflict when sold out."""

    @abstractmethod
    def confirm_hold(self, hold_id: UUID) -> None: ...

    @abstractmethod
    def release_hold(self, hold_id: UUID, reason: HoldReleaseReason) -> None:
        """Give the rooms back; idempotent."""


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    booking_id: UUID
    payer_id: str
    amount: Money
    method: str
    method_details: Mapping[str, Any] = field(repr=False)
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class ChargeResult:
    payment_id: UUID
    succeeded: bool
    status: str
    instrument_display: str
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class RefundRequest:
    payment_id: UUID
    amount: Money
    reason: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RefundResult:
    refund_id: UUID


class PaymentPort(ABC):
    @abstractmethod
    def find_charge(self, idempotency_key: str) -> ChargeResult | None:
        """The charge already made under ``idempotency_key``, from Postgres: replay works without Redis."""

    @abstractmethod
    def charge(self, request: ChargeRequest) -> ChargeResult:
        """Charge the amount; a decline is a result (``succeeded=False``), not an error."""

    @abstractmethod
    def refund(self, request: RefundRequest) -> RefundResult:
        """Refund part or all of a payment; a gateway failure raises UpstreamFailure(REFUND_FAILED)."""
