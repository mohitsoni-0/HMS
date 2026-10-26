"""In-memory test doubles for the booking services: the repository, the four ports and a unit of work.

``SnapshotUnitOfWork`` snapshots every store it is given when a block is entered and restores them when the
block raises, so unit tests can prove that a failed use case leaves no trace, as ``transaction.atomic()``
does. Stores hand out copies, so an entity changed but never saved is not persisted, as with the ORM.
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol
from uuid import UUID, uuid4

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict, NotFound, UpstreamFailure
from hotel_commons.status_code import RESOURCE_NOT_FOUND, StatusCode

from booking.domain.booking import Booking
from booking.domain.value_objects import BookingPrice, RoomTypeInfo
from booking.enums import BookingStatus
from booking.interfaces.ports import (
    CatalogPort,
    ChargeRequest,
    ChargeResult,
    InventoryPort,
    PaymentPort,
    PricingPort,
    RefundRequest,
    RefundResult,
)
from booking.interfaces.repositories import BookingRepository
from booking.status_code import BOOKING_NOT_FOUND

# Stand-ins for the other contexts' codes: booking tests never import another app's internals.
HOLD_NOT_CONFIRMABLE = StatusCode(2199001, "Hold is {state}", 409)
HOLD_RELEASE_FAILED = StatusCode(2199002, "Hold could not be released", 409)
REFUND_FAILED = StatusCode(2199003, "Refund could not be processed, please retry", 502)

DECLINED_CARD_SUFFIX = "0002"


class Snapshottable(Protocol):
    def snapshot(self) -> object: ...

    def restore(self, state: object) -> None: ...


class SnapshotUnitOfWork:
    """A unit of work over in-memory stores: snapshot on enter, restore when the block raises.

    Nested blocks keep their own snapshots, like savepoints. ``enters``, ``commits`` and ``rollbacks`` count
    the blocks so tests can assert how a use case used its transactions.
    """

    def __init__(self, *stores: Snapshottable) -> None:
        self._stores = stores
        self._snapshots: list[list[object]] = []
        self.enters = 0
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "SnapshotUnitOfWork":
        self.enters += 1
        self._snapshots.append([store.snapshot() for store in self._stores])
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        states = self._snapshots.pop()
        if exc_type is None:
            self.commits += 1
            return
        self.rollbacks += 1
        for store, state in zip(self._stores, states, strict=True):
            store.restore(state)


class InMemoryBookingRepository(BookingRepository):
    """Bookings in a dict. ``locked_elsewhere`` simulates rows another worker holds (SKIP LOCKED)."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Booking] = {}
        self.locked_elsewhere: set[UUID] = set()

    def snapshot(self) -> object:
        return copy.deepcopy(self._rows)

    def restore(self, state: object) -> None:
        self._rows = copy.deepcopy(state)

    def add(self, booking: Booking) -> None:
        if booking.id in self._rows:
            raise AssertionError(f"booking {booking.id} added twice")
        self._rows[booking.id] = copy.deepcopy(booking)

    def get(self, booking_id: UUID) -> Booking:
        if booking_id not in self._rows:
            raise NotFound(BOOKING_NOT_FOUND)
        return copy.deepcopy(self._rows[booking_id])

    def get_for_update(self, booking_id: UUID) -> Booking:
        return self.get(booking_id)

    def save(self, booking: Booking) -> None:
        if booking.id not in self._rows:
            raise AssertionError(f"booking {booking.id} saved before it was added")
        self._rows[booking.id] = copy.deepcopy(booking)

    def list_for_guest(
        self, guest_id: str, *, page: int, page_size: int, property_id: UUID | None = None
    ) -> tuple[list[Booking], int]:
        return self._page(
            [b for b in self._rows.values() if b.guest_id == guest_id and property_id in (None, b.property_id)],
            page,
            page_size,
        )

    def list_for_property(self, property_id: UUID, *, page: int, page_size: int) -> tuple[list[Booking], int]:
        return self._page([b for b in self._rows.values() if b.property_id == property_id], page, page_size)

    def find_expired_pending_ids(self, now: datetime, limit: int) -> list[UUID]:
        expired = sorted(
            (b for b in self._rows.values() if _is_expired_pending(b, now)), key=lambda b: b.hold_expires_at
        )
        return [booking.id for booking in expired[:limit]]

    def lock_if_expired_pending(self, booking_id: UUID, now: datetime) -> Booking | None:
        booking = self._rows.get(booking_id)
        if booking_id in self.locked_elsewhere or booking is None or not _is_expired_pending(booking, now):
            return None
        return copy.deepcopy(booking)

    @staticmethod
    def _page(bookings: list[Booking], page: int, page_size: int) -> tuple[list[Booking], int]:
        newest_first = sorted(bookings, key=lambda b: b.created_at, reverse=True)
        start = (page - 1) * page_size
        return [copy.deepcopy(b) for b in newest_first[start : start + page_size]], len(bookings)


class FakeCatalog(CatalogPort):
    def __init__(self, *room_types: RoomTypeInfo) -> None:
        self.room_types = {room_type.room_type_id: room_type for room_type in room_types}

    def room_type_info(self, room_type_id: UUID) -> RoomTypeInfo:
        if room_type_id not in self.room_types:
            raise NotFound(RESOURCE_NOT_FOUND, resource="Room type")
        return self.room_types[room_type_id]


class FakePricing(PricingPort):
    """The base price every night; records each call so tests can prove pricing was (not) reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, DateRange, int]] = []

    def quote(self, room_type: RoomTypeInfo, stay: DateRange, rooms: int) -> BookingPrice:
        self.calls.append((room_type.room_type_id, stay, rooms))
        return BookingPrice(
            nightly_rates=tuple((night, room_type.base_price) for night in stay.dates()),
            rooms=rooms,
            total=room_type.base_price.times(stay.nights * rooms),
            pricing_strategy=room_type.pricing_strategy,
        )


@dataclass(frozen=True, slots=True)
class PlacedHold:
    room_type_id: UUID
    stay: DateRange
    rooms: int
    reference: str
    expires_at: datetime | None


class FakeInventory(InventoryPort):
    """Holds as ``{hold_id: "HELD" | "CONFIRMED" | "RELEASED"}``; ``failing_releases`` makes a release raise."""

    def __init__(self) -> None:
        self.statuses: dict[UUID, str] = {}
        self.release_reasons: dict[UUID, str] = {}
        self.placed: dict[UUID, PlacedHold] = {}
        self.failing_releases: set[UUID] = set()

    def snapshot(self) -> object:
        return copy.deepcopy((self.statuses, self.release_reasons, self.placed))

    def restore(self, state: object) -> None:
        self.statuses, self.release_reasons, self.placed = copy.deepcopy(state)

    def hold(
        self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> UUID:
        hold_id = uuid4()
        self.statuses[hold_id] = "HELD"
        self.placed[hold_id] = PlacedHold(room_type_id, stay, rooms, reference, expires_at)
        return hold_id

    def confirm_hold(self, hold_id: UUID) -> None:
        if self.statuses[hold_id] == "RELEASED":
            raise Conflict(HOLD_NOT_CONFIRMABLE, state="RELEASED")
        self.statuses[hold_id] = "CONFIRMED"

    def release_hold(self, hold_id: UUID, reason: Literal["CANCELLED", "EXPIRED"]) -> None:
        if hold_id in self.failing_releases:
            raise Conflict(HOLD_RELEASE_FAILED)
        if self.statuses[hold_id] != "RELEASED":
            self.statuses[hold_id] = "RELEASED"
            self.release_reasons[hold_id] = reason


@dataclass
class FakePayments(PaymentPort):
    """Idempotent by key like the payment context. A card ending 0002 is declined; ``fail_refunds`` makes the
    gateway reject refunds. The request logs survive a rollback (they record calls); the rows do not."""

    charges: dict[str, ChargeResult] = field(default_factory=dict)
    refunds: dict[str, RefundResult] = field(default_factory=dict)
    charge_requests: list[ChargeRequest] = field(default_factory=list)
    refund_requests: list[RefundRequest] = field(default_factory=list)
    fail_refunds: bool = False

    def snapshot(self) -> object:
        return copy.deepcopy((self.charges, self.refunds))

    def restore(self, state: object) -> None:
        self.charges, self.refunds = copy.deepcopy(state)

    def find_charge(self, idempotency_key: str) -> ChargeResult | None:
        return self.charges.get(idempotency_key)

    def charge(self, request: ChargeRequest) -> ChargeResult:
        self.charge_requests.append(request)
        if request.idempotency_key not in self.charges:
            self.charges[request.idempotency_key] = _gateway_result(request)
        return self.charges[request.idempotency_key]

    def refund(self, request: RefundRequest) -> RefundResult:
        self.refund_requests.append(request)
        if self.fail_refunds:
            raise UpstreamFailure(REFUND_FAILED)
        return self.refunds.setdefault(request.idempotency_key, RefundResult(refund_id=uuid4()))


def _gateway_result(request: ChargeRequest) -> ChargeResult:
    card_number = str(request.method_details.get("card_number", ""))
    if card_number.endswith(DECLINED_CARD_SUFFIX):
        return ChargeResult(
            payment_id=uuid4(),
            succeeded=False,
            status="FAILED",
            instrument_display=f"VISA •••• {card_number[-4:]}",
            failure_code="card_declined",
            failure_message="The card was declined",
        )
    return ChargeResult(
        payment_id=uuid4(),
        succeeded=True,
        status="SUCCEEDED",
        instrument_display=f"VISA •••• {card_number[-4:]}",
        failure_code=None,
        failure_message=None,
    )


def _is_expired_pending(booking: Booking, now: datetime) -> bool:
    return booking.status is BookingStatus.PENDING_PAYMENT and booking.hold_expired(now)
