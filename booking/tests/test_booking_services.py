"""Booking use cases (LLD §8.4) over in-memory fakes: create, pay, cancel and the expiry job.

No Django and no database: ``SnapshotUnitOfWork`` rolls the fakes back when a use case raises, so these
tests also prove what a failure leaves behind.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation, Conflict, UpstreamFailure, ValidationFailed
from hotel_commons.status_code import IDEMPOTENCY_KEY_REUSED
from hotel_commons.testing.clock import FixedClock
from hotel_commons.testing.events import RecordingEventPublisher

from booking.domain.booking import Booking
from booking.domain.booking_types import BookingTypeRegistry, PayAtPropertyBookingPolicy, PrepaidBookingPolicy
from booking.domain.cancellation import CancellationPolicyRegistry
from booking.domain.value_objects import GuestContact, RoomTypeInfo
from booking.interfaces.repositories import DuplicateIdempotencyKey
from booking.enums import BookingRefundStatus, BookingStatus, BookingType
from booking.public.events import BookingCancelled, BookingConfirmed, BookingCreated, BookingExpired
from booking.services.cancel_booking import CancelBookingService
from booking.services.commands import CancelBookingCommand, CreateBookingCommand, PayBookingCommand
from booking.services.create_booking import CreateBookingService
from booking.services.expire_bookings import ExpireBookingsService
from booking.services.pay_booking import PayBookingService
from booking.services.settings import BookingSettings
from booking.status_code import (
    BOOKING_NOT_PAYABLE,
    CANCELLATION_WINDOW_CLOSED,
    GUEST_CAPACITY_EXCEEDED,
    INVALID_BOOKING_REQUEST,
    INVALID_STATE_TRANSITION,
)
from booking.tests.fakes import (
    HOLD_NOT_CONFIRMABLE,
    FakeCatalog,
    FakeInventory,
    FakePayments,
    FakePricing,
    InMemoryBookingRepository,
    SnapshotUnitOfWork,
)

NOW = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)  # Monday 12:00 in Asia/Kolkata
CHECK_IN = date(2026, 1, 15)
CHECK_OUT = date(2026, 1, 17)
CHECK_IN_AT = datetime(2026, 1, 15, 8, 30, tzinfo=UTC)  # 14:00 in Asia/Kolkata
HOLD_TTL = timedelta(minutes=15)
NIGHTLY = Money.of_rupees("4000")
GUEST = "guest-1"
GOOD_CARD = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2028, "cvv": "123"}
DECLINED_CARD = {**GOOD_CARD, "card_number": "4000000000000002"}
CONTACT = GuestContact(name="Rahul Sharma", email="rahul@example.com", phone="9876543210")


def room_type(**overrides: object) -> RoomTypeInfo:
    values = {
        "room_type_id": uuid4(),
        "property_id": uuid4(),
        "owner_account_id": uuid4(),
        "max_occupancy": 2,
        "total_rooms": 5,
        "base_price": NIGHTLY,
        "pricing_strategy": PricingStrategyCode.STANDARD,
        "cancellation_policy": CancellationPolicyCode.FLEXIBLE,
        "check_in_time": time(14, 0),
        "is_active": True,
    }
    return RoomTypeInfo(**{**values, **overrides})


class World:
    """The booking services wired to fakes, sharing one clock, event recorder and unit of work."""

    def __init__(self, *room_types: RoomTypeInfo, booking_types: BookingTypeRegistry | None = None) -> None:
        self.clock = FixedClock(NOW)
        self.events = RecordingEventPublisher()
        self.bookings = InMemoryBookingRepository()
        self.catalog = FakeCatalog(*room_types)
        self.pricing = FakePricing()
        self.inventory = FakeInventory()
        self.payments = FakePayments()
        self.uow = SnapshotUnitOfWork(self.bookings, self.inventory, self.payments)
        self.settings = BookingSettings(hold_ttl=HOLD_TTL, max_nights=30, horizon_days=365)
        self.booking_types = booking_types or BookingTypeRegistry()
        self.create_service = CreateBookingService(
            self.bookings,
            self.catalog,
            self.pricing,
            self.inventory,
            self.booking_types,
            self.events,
            self.uow,
            self.clock,
            self.settings,
        )
        self.pay_service = PayBookingService(
            self.bookings, self.payments, self.inventory, self.booking_types, self.events, self.uow, self.clock
        )
        self.cancel_service = CancelBookingService(
            self.bookings,
            self.inventory,
            self.payments,
            CancellationPolicyRegistry(),
            self.events,
            self.uow,
            self.clock,
        )
        self.expire_service = ExpireBookingsService(self.bookings, self.inventory, self.events, self.uow, self.clock)

    def create(self, room: RoomTypeInfo, **overrides: object):
        return self.create_result(room, **overrides).booking

    def create_result(self, room: RoomTypeInfo, **overrides: object):
        values = {
            "guest_id": GUEST,
            "room_type_id": room.room_type_id,
            "check_in": CHECK_IN,
            "check_out": CHECK_OUT,
            "rooms": 1,
            "guests": 2,
            "booking_type": BookingType.PREPAID,
            "contact": CONTACT,
        }
        return self.create_service.execute(CreateBookingCommand(**{**values, **overrides}))

    def pay(
        self, booking_id: UUID, key: str = "pay-attempt-0001", card: dict | None = None, method: str = "CARD"
    ):
        return self.pay_service.execute(
            PayBookingCommand(
                booking_id=booking_id,
                payer_id=GUEST,
                method=method,
                method_details=card or GOOD_CARD,
                idempotency_key=key,
            )
        )

    def cancel(self, booking_id: UUID, reason: str | None = "plans changed"):
        return self.cancel_service.execute(CancelBookingCommand(booking_id=booking_id, actor_id=GUEST, reason=reason))


@pytest.fixture
def room() -> RoomTypeInfo:
    return room_type()


@pytest.fixture
def world(room: RoomTypeInfo) -> World:
    return World(room)


def test_create_pay_confirm(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)

    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.price.total == NIGHTLY.times(2)
    assert booking.hold_expires_at == NOW + HOLD_TTL
    assert world.inventory.statuses[booking.hold_id] == "HELD"
    assert world.inventory.placed[booking.hold_id].reference == str(booking.id)
    assert world.inventory.placed[booking.hold_id].expires_at == NOW + HOLD_TTL
    assert [type(event) for event in world.events.events] == [BookingCreated]

    result = world.pay(booking.id)

    assert result.outcome == "PAID"
    assert result.booking.status is BookingStatus.CONFIRMED
    assert result.booking.payment_id == result.payment.payment_id
    assert result.booking.confirmed_at == NOW
    [charge] = world.payments.charge_requests
    assert charge.amount == booking.price.total
    assert charge.idempotency_key == f"pay:{booking.id}:pay-attempt-0001"
    assert world.inventory.statuses[booking.hold_id] == "CONFIRMED"
    assert world.bookings.get(booking.id).status is BookingStatus.CONFIRMED
    confirmed = world.events.of_type(BookingConfirmed)
    assert [(e.booking_id, e.guest_id, e.property_id) for e in confirmed] == [(booking.id, GUEST, room.property_id)]


def test_declined_payment_keeps_pending_outcome(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)

    declined = world.pay(booking.id, key="pay-attempt-0001", card=DECLINED_CARD)

    assert declined.outcome == "DECLINED"
    assert declined.payment.failure_code == "card_declined"
    stored = world.bookings.get(booking.id)
    assert stored.status is BookingStatus.PENDING_PAYMENT
    assert stored.last_payment_failure == "card_declined"
    assert world.inventory.statuses[booking.hold_id] == "HELD"
    assert world.uow.rollbacks == 0  # the failure is committed, not raised

    retried = world.pay(booking.id, key="pay-attempt-0002")

    assert retried.outcome == "PAID"
    assert world.bookings.get(booking.id).last_payment_failure is None


def test_hold_expired_at_exact_boundary(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    assert not booking.hold_expired(NOW + HOLD_TTL - timedelta(microseconds=1))
    assert booking.hold_expired(NOW + HOLD_TTL)
    world.clock.advance(HOLD_TTL)

    result = world.pay(booking.id)

    assert result.outcome == "EXPIRED"
    assert result.payment is None
    assert world.payments.charge_requests == []
    stored = world.bookings.get(booking.id)
    assert stored.status is BookingStatus.EXPIRED
    assert stored.expired_at == NOW + HOLD_TTL
    assert world.inventory.statuses[booking.hold_id] == "RELEASED"
    assert world.inventory.release_reasons[booking.hold_id] == "EXPIRED"
    assert len(world.events.of_type(BookingExpired)) == 1


def test_pay_after_job_expired_returns_expired(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.clock.advance(HOLD_TTL + timedelta(minutes=1))
    assert world.expire_service.execute() == 1

    result = world.pay(booking.id)

    assert result.outcome == "EXPIRED"
    assert result.booking.status is BookingStatus.EXPIRED
    assert world.payments.charge_requests == []
    assert len(world.events.of_type(BookingExpired)) == 1  # the job's, not a second one


def test_pay_replays_prior_charge_without_redis(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    first = world.pay(booking.id, key="pay-attempt-0001")

    # Redis forgot the key: the request reaches the service again, now for a CONFIRMED booking.
    replayed = world.pay(booking.id, key="pay-attempt-0001")

    assert replayed.outcome == "PAID"
    assert replayed.payment == first.payment
    assert len(world.payments.charge_requests) == 1
    assert len(world.events.of_type(BookingConfirmed)) == 1


def test_pay_replays_prior_decline_as_declined(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.pay(booking.id, key="pay-attempt-0001", card=DECLINED_CARD)

    assert world.pay(booking.id, key="pay-attempt-0001", card=DECLINED_CARD).outcome == "DECLINED"
    assert len(world.payments.charge_requests) == 1


def test_pay_key_reused_for_another_method_is_refused_without_redis(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.pay(booking.id, key="pay-attempt-0001", card=DECLINED_CARD)

    # Redis forgot the key; the client retries it with a UPI payment instead of the declined card.
    with pytest.raises(BusinessRuleViolation) as raised:
        world.pay(booking.id, key="pay-attempt-0001", method="UPI", card={"vpa": "rahul@okbank"})

    assert raised.value.status_code is IDEMPOTENCY_KEY_REUSED
    assert len(world.payments.charge_requests) == 1  # UPI was not charged under the card's key
    assert world.bookings.get(booking.id).status is BookingStatus.PENDING_PAYMENT


def test_pay_with_new_key_after_confirmation_conflicts(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.pay(booking.id, key="pay-attempt-0001")

    with pytest.raises(Conflict) as raised:
        world.pay(booking.id, key="pay-attempt-0002")

    assert raised.value.status_code is INVALID_STATE_TRANSITION
    assert raised.value.message == "Cannot pay a booking that is CONFIRMED"


@pytest.mark.parametrize(
    ("policy", "lead_time", "percent"),
    [
        (CancellationPolicyCode.FLEXIBLE, timedelta(hours=48), 100),
        (CancellationPolicyCode.MODERATE, timedelta(hours=72), 50),
    ],
)
def test_cancel_confirmed_refunds_by_policy(
    policy: CancellationPolicyCode, lead_time: timedelta, percent: int
) -> None:
    room = room_type(cancellation_policy=policy)
    world = World(room)
    booking = world.create(room)
    payment = world.pay(booking.id).payment
    world.clock.advance(CHECK_IN_AT - lead_time - NOW)

    cancelled = world.cancel(booking.id)

    expected_refund = booking.price.total.percent(Decimal(percent))
    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.cancelled_at == CHECK_IN_AT - lead_time
    assert cancelled.cancellation_reason == "plans changed"
    assert cancelled.refund_amount == expected_refund
    assert cancelled.refund_percent == Decimal(percent)
    assert cancelled.refund_status is BookingRefundStatus.REFUNDED
    [refund] = world.payments.refund_requests
    assert (refund.payment_id, refund.amount) == (payment.payment_id, expected_refund)
    assert refund.idempotency_key == f"refund:{booking.id}"
    assert cancelled.refund_id == world.payments.refunds[refund.idempotency_key].refund_id
    assert world.inventory.release_reasons[booking.hold_id] == "CANCELLED"
    assert world.bookings.get(booking.id).status is BookingStatus.CANCELLED
    assert len(world.events.of_type(BookingCancelled)) == 1


def test_cancel_without_refund_due_skips_the_gateway() -> None:
    room = room_type(cancellation_policy=CancellationPolicyCode.STRICT)
    world = World(room)
    booking = world.create(room)
    world.pay(booking.id)
    world.clock.advance(CHECK_IN_AT - timedelta(hours=100) - NOW)  # inside STRICT's 168-hour minimum

    cancelled = world.cancel(booking.id)

    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.refund_amount == Money.zero()
    assert cancelled.refund_status is BookingRefundStatus.NONE
    assert world.payments.refund_requests == []


def test_cancel_pending_booking_releases_hold_without_refund(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)

    cancelled = world.cancel(booking.id, reason=None)

    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.refund_amount == Money.zero()
    assert world.payments.refund_requests == []
    assert world.inventory.release_reasons[booking.hold_id] == "CANCELLED"


def test_refund_failure_rolls_back_cancel(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.pay(booking.id)
    world.payments.fail_refunds = True
    events_before = len(world.events.events)

    with pytest.raises(UpstreamFailure):
        world.cancel(booking.id)

    stored = world.bookings.get(booking.id)
    assert stored.status is BookingStatus.CONFIRMED
    assert stored.cancelled_at is None
    assert stored.refund_amount is None
    assert world.inventory.statuses[booking.hold_id] == "CONFIRMED"
    assert world.payments.refunds == {}
    assert len(world.events.events) == events_before
    assert world.uow.rollbacks == 1

    world.payments.fail_refunds = False
    assert world.cancel(booking.id).status is BookingStatus.CANCELLED  # the client simply retries


@pytest.mark.parametrize("after_check_in", [timedelta(0), timedelta(hours=3)])
def test_cancel_after_check_in_rejected(world: World, room: RoomTypeInfo, after_check_in: timedelta) -> None:
    booking = world.create(room)
    world.pay(booking.id)
    assert booking.check_in_at() == CHECK_IN_AT
    world.clock.advance(CHECK_IN_AT + after_check_in - NOW)

    with pytest.raises(BusinessRuleViolation) as raised:
        world.cancel(booking.id)

    assert raised.value.status_code is CANCELLATION_WINDOW_CLOSED
    assert world.bookings.get(booking.id).status is BookingStatus.CONFIRMED
    assert world.payments.refund_requests == []


def test_cancel_twice_conflicts(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room)
    world.cancel(booking.id)

    with pytest.raises(Conflict) as raised:
        world.cancel(booking.id)

    assert raised.value.message == "Cannot cancel a booking that is CANCELLED"


def test_pay_at_property_confirmed_without_payment(world: World, room: RoomTypeInfo) -> None:
    booking = world.create(room, booking_type=BookingType.PAY_AT_PROPERTY)

    assert booking.status is BookingStatus.CONFIRMED
    assert booking.hold_expires_at is None
    assert booking.payment_id is None
    assert world.inventory.statuses[booking.hold_id] == "CONFIRMED"
    assert world.inventory.placed[booking.hold_id].expires_at is None

    with pytest.raises(BusinessRuleViolation) as raised:
        world.pay(booking.id)
    assert raised.value.status_code is BOOKING_NOT_PAYABLE

    cancelled = world.cancel(booking.id)
    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.refund_amount == Money.zero()
    assert cancelled.refund_status is BookingRefundStatus.NONE
    assert world.payments.charge_requests == []
    assert world.payments.refund_requests == []


def test_guest_capacity_exceeded(world: World, room: RoomTypeInfo) -> None:
    with pytest.raises(BusinessRuleViolation) as raised:
        world.create(room, rooms=1, guests=3)

    assert raised.value.status_code is GUEST_CAPACITY_EXCEEDED
    assert raised.value.message == "3 guests exceed capacity of 2"
    assert world.pricing.calls == []
    assert world.inventory.placed == {}

    assert world.create(room, rooms=2, guests=3).guests == 3  # two rooms hold four


def test_validate_before_pricing(world: World, room: RoomTypeInfo) -> None:
    with pytest.raises(ValidationFailed) as raised:
        world.create(room, check_out=CHECK_IN + timedelta(days=31))

    assert raised.value.status_code is INVALID_BOOKING_REQUEST
    assert world.pricing.calls == []
    assert world.inventory.placed == {}
    assert world.bookings.list_for_guest(GUEST, page=1, page_size=20) == ([], 0)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"check_in": NOW.date() - timedelta(days=1), "check_out": NOW.date() + timedelta(days=1)}, ValidationFailed),
        ({"check_in": date(2027, 1, 4), "check_out": date(2027, 1, 6)}, ValidationFailed),  # beyond the horizon
        ({"rooms": 6}, ValidationFailed),  # more than the room type has
        ({"rooms": 0}, ValidationFailed),
    ],
)
def test_invalid_requests_are_rejected_before_any_hold(
    world: World, room: RoomTypeInfo, overrides: dict, error: type[Exception]
) -> None:
    with pytest.raises(error):
        world.create(room, **overrides)

    assert world.pricing.calls == []
    assert world.inventory.placed == {}


def test_inactive_room_type_is_not_bookable() -> None:
    room = room_type(is_active=False)
    world = World(room)

    with pytest.raises(BusinessRuleViolation):
        world.create(room)

    assert world.pricing.calls == []


def test_expire_job_handles_each_booking_separately(world: World, room: RoomTypeInfo) -> None:
    failing, locked, healthy = (world.create(room) for _ in range(3))
    world.clock.advance(timedelta(minutes=5))
    still_held = world.create(room)  # its hold runs until 5 minutes after the others
    world.clock.advance(HOLD_TTL - timedelta(minutes=5))
    world.inventory.failing_releases.add(failing.hold_id)
    world.bookings.locked_elsewhere.add(locked.id)
    entered_before = world.uow.enters

    expired = world.expire_service.execute(batch_size=10)

    assert expired == 1
    assert world.uow.enters - entered_before == 3  # one unit of work per expired booking found
    assert world.uow.rollbacks == 1  # only the failing booking's
    assert world.bookings.get(healthy.id).status is BookingStatus.EXPIRED
    assert world.inventory.release_reasons[healthy.hold_id] == "EXPIRED"
    assert world.bookings.get(failing.id).status is BookingStatus.PENDING_PAYMENT
    assert world.bookings.get(locked.id).status is BookingStatus.PENDING_PAYMENT
    assert world.bookings.get(still_held.id).status is BookingStatus.PENDING_PAYMENT
    assert [event.booking_id for event in world.events.of_type(BookingExpired)] == [healthy.id]


def test_expire_job_stops_after_its_time_budget(world: World, room: RoomTypeInfo) -> None:
    for _ in range(3):
        world.create(room)
    world.clock.advance(HOLD_TTL)

    assert world.expire_service.execute(batch_size=2, time_budget=timedelta(0)) == 2  # one batch, then stop
    assert world.expire_service.execute(batch_size=2, time_budget=timedelta(0)) == 1
    assert world.expire_service.execute(batch_size=2, time_budget=timedelta(0)) == 0


def test_expire_job_drains_the_backlog_in_one_run(world: World, room: RoomTypeInfo) -> None:
    bookings = [world.create(room) for _ in range(5)]
    world.clock.advance(HOLD_TTL)

    assert world.expire_service.execute(batch_size=2) == 5  # batches of 2, 2 and a short 1

    assert all(world.bookings.get(b.id).status is BookingStatus.EXPIRED for b in bookings)


def test_expire_job_does_not_retry_failures_within_a_run(world: World, room: RoomTypeInfo) -> None:
    failing = [world.create(room) for _ in range(2)]  # the oldest holds: first in every batch
    world.clock.advance(timedelta(minutes=1))
    healthy = [world.create(room) for _ in range(2)]
    world.clock.advance(HOLD_TTL)
    world.inventory.failing_releases.update(b.hold_id for b in failing)

    assert world.expire_service.execute(batch_size=2) == 2

    assert all(world.bookings.get(b.id).status is BookingStatus.EXPIRED for b in healthy)
    assert all(world.bookings.get(b.id).status is BookingStatus.PENDING_PAYMENT for b in failing)
    assert world.uow.rollbacks == 2  # each failing booking was tried once


def test_guest_contact_hides_pii_from_repr() -> None:
    text = repr(CONTACT)

    assert "Rahul" not in text and "rahul@example.com" not in text and "9876543210" not in text


@pytest.mark.parametrize(
    "contact",
    [
        {"name": "R", "email": "rahul@example.com", "phone": "9876543210"},
        {"name": "Rahul", "email": "not-an-email", "phone": "9876543210"},
        {"name": "Rahul", "email": "rahul@example.com", "phone": "1234567890"},
    ],
)
def test_guest_contact_is_validated(contact: dict[str, str]) -> None:
    with pytest.raises(ValidationFailed):
        GuestContact(**contact)


def test_create_with_same_key_returns_the_first_booking(world: World, room: RoomTypeInfo) -> None:
    first = world.create_result(room, idempotency_key="create-attempt-0001")

    # Redis was down or forgot the key: the retry reaches the service again.
    again = world.create_result(room, idempotency_key="create-attempt-0001")

    assert (first.replayed, again.replayed) == (False, True)
    assert again.booking.id == first.booking.id
    assert len(world.inventory.placed) == 1  # one hold, not two
    assert len(world.events.of_type(BookingCreated)) == 1
    assert world.bookings.list_for_guest(GUEST, page=1, page_size=20)[1] == 1


def test_create_key_reused_for_another_stay_is_refused(world: World, room: RoomTypeInfo) -> None:
    world.create(room, idempotency_key="create-attempt-0001")

    with pytest.raises(BusinessRuleViolation) as raised:
        world.create(room, idempotency_key="create-attempt-0001", rooms=2)

    assert raised.value.status_code is IDEMPOTENCY_KEY_REUSED
    assert len(world.inventory.placed) == 1


def test_create_keys_are_per_guest(world: World, room: RoomTypeInfo) -> None:
    mine = world.create(room, idempotency_key="create-attempt-0001")
    theirs = world.create(room, idempotency_key="create-attempt-0001", guest_id="guest-2")

    assert theirs.id != mine.id


def test_create_losing_the_insert_race_returns_the_winner(world: World, room: RoomTypeInfo) -> None:
    winner = world.create(room, idempotency_key="create-attempt-0001")
    real_lookup = world.bookings.find_by_idempotency_key
    calls: list[tuple[str, str]] = []

    def racing_lookup(guest_id: str, key: str) -> Booking | None:
        calls.append((guest_id, key))
        return None if len(calls) == 1 else real_lookup(guest_id, key)  # looked before the winner committed

    world.bookings.find_by_idempotency_key = racing_lookup

    result = world.create_result(room, idempotency_key="create-attempt-0001")

    assert result.replayed is True
    assert result.booking.id == winner.id
    assert list(world.inventory.placed) == [winner.hold_id]  # the loser's hold was rolled back
    assert world.uow.rollbacks == 1


def test_create_losing_the_last_rooms_to_its_own_retry_returns_the_winner(world: World, room: RoomTypeInfo) -> None:
    winner = world.create(room, idempotency_key="create-attempt-0001")
    real_lookup = world.bookings.find_by_idempotency_key
    calls: list[tuple[str, str]] = []

    def racing_lookup(guest_id: str, key: str) -> Booking | None:
        calls.append((guest_id, key))
        return None if len(calls) == 1 else real_lookup(guest_id, key)

    def sold_out(**kwargs: object) -> UUID:
        raise Conflict(HOLD_NOT_CONFIRMABLE, state="SOLD_OUT")  # the winner took the last rooms

    world.bookings.find_by_idempotency_key = racing_lookup
    world.inventory.hold = sold_out

    result = world.create_result(room, idempotency_key="create-attempt-0001")

    assert (result.replayed, result.booking.id) == (True, winner.id)
    with pytest.raises(Conflict):
        world.create(room)  # without a key a sold-out stay is still a 409


def test_create_without_key_is_not_deduplicated(world: World, room: RoomTypeInfo) -> None:
    assert world.create(room).id != world.create(room).id


def test_repository_refuses_a_second_booking_under_the_same_key(world: World, room: RoomTypeInfo) -> None:
    first = world.create(room, idempotency_key="create-attempt-0001")
    second = world.create(room)
    second.idempotency_key = "create-attempt-0001"
    second.id = uuid4()

    with pytest.raises(DuplicateIdempotencyKey):
        world.bookings.add(second)

    assert world.bookings.find_by_idempotency_key(GUEST, "create-attempt-0001").id == first.id


class _InvoicedPolicy(PrepaidBookingPolicy):
    """A type that is never paid online, yet starts pending (e.g. a corporate invoice)."""

    requires_online_payment = False


class _CommittedPrepaidPolicy(PrepaidBookingPolicy):
    """A prepaid type whose rooms are committed at once although the booking awaits payment."""

    confirms_hold_on_create = True


def test_pay_asks_the_type_policy_whether_it_is_payable(room: RoomTypeInfo) -> None:
    world = World(room, booking_types=BookingTypeRegistry([_InvoicedPolicy(), PayAtPropertyBookingPolicy()]))
    booking = world.create(room)

    with pytest.raises(BusinessRuleViolation) as raised:
        world.pay(booking.id)

    assert raised.value.status_code is BOOKING_NOT_PAYABLE
    assert world.payments.charge_requests == []


def test_create_asks_the_type_policy_whether_to_confirm_the_hold(room: RoomTypeInfo) -> None:
    world = World(room, booking_types=BookingTypeRegistry([_CommittedPrepaidPolicy(), PayAtPropertyBookingPolicy()]))

    booking = world.create(room)

    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert world.inventory.statuses[booking.hold_id] == "CONFIRMED"


def test_guest_contact_uses_the_shared_email_rule() -> None:
    with pytest.raises(ValidationFailed):
        GuestContact(name="Rahul", email="a@mail.co.", phone="9876543210")  # catalog already refused this
