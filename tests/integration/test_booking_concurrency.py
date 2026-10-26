"""Booking-level races decided under the booking row lock (HLD §9, LLD §8.4, §14 "Concurrency (db)").

The unit tests run the booking services on in-memory fakes, which have no locks, so only these tests would
notice ``BookingRepository.get_for_update`` losing its ``FOR UPDATE`` or the expiry job losing
``SKIP LOCKED``. Each racing thread sends its request over the public HTTP API on its own database connection
(the expiry job runs as the scheduler would run it), so everything really commits: the tests use
``transaction=True`` and ``truncate_context_tables`` empties the context tables afterwards.

Regressions caught, per test:
- pay racing the expiry job: a booking CONFIRMED on a released hold, or EXPIRED after a successful charge;
- double cancel: two refunds, or two releases of the same rooms;
- two pays with different Idempotency-Keys: two charges for one booking;
- the last room booked by many guests at once: more than one booking (booking row and hold in one transaction).
"""

import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone
from hotel_commons.db.unit_of_work import DjangoUnitOfWork
from hotel_commons.domain.clock import SystemClock
from hotel_commons.events.in_process import event_bus
from hotel_commons.testing.clock import FixedClock
from hotel_commons.testing.tokens import auth_header
from rest_framework.test import APIClient

from booking.adapters.inventory_adapter import InventoryAdapter
from booking.enums import BookingStatus
from booking.models.booking_models import Booking as BookingRow
from booking.repositories.booking_repository import DjangoBookingRepository
from booking.services.expire_bookings import ExpireBookingsService
from inventory.enums import HoldStatus
from inventory.models.inventory_models import InventoryHold, RoomNight
from payment.enums import PaymentStatus
from payment.models.payment_models import Payment as PaymentRow
from payment.models.payment_models import Refund as RefundRow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("truncate_context_tables"),
]

API = "/hotel/v1"
OWNER = "owner-race"
ROUNDS = 8
# The in-process job reaches the row lock sooner than an HTTP payment does, so its start is staggered across
# rounds: both orders get exercised, including the job finding the row locked by the payment (SKIP LOCKED).
JOB_DELAYS_SECONDS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.2)
LAST_ROOM_GUESTS = 10
JOIN_TIMEOUT_SECONDS = 30
CARD = {
    "card_number": "4242424242424242",
    "expiry_month": 12,
    "expiry_year": SystemClock().today().year + 2,  # validated against the real clock, so never hard-coded
    "cvv": "123",
    "holder_name": "Rahul Sharma",
}
CONTACT = {"guest_name": "Rahul Sharma", "guest_email": "rahul@example.com", "guest_phone": "9876543210"}

# Status codes asserted below
SUCCESS = 2100000
INVENTORY_UNAVAILABLE = 2102001
INVALID_STATE_TRANSITION = 2105002
BOOKING_EXPIRED = 2105003

Outcome = tuple[str, Any]  # ("ok", value) or ("error", repr of the exception)


def _race(attempts: list[Callable[[], Any]]) -> list[Outcome]:
    """Run the attempts on their own threads, released together by a barrier; one outcome per attempt."""
    barrier = threading.Barrier(len(attempts))
    results: list[Outcome] = [("error", "did not finish")] * len(attempts)

    def run(index: int, attempt: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=JOIN_TIMEOUT_SECONDS)
            results[index] = ("ok", attempt())
        except Exception as exc:  # noqa: BLE001 - any exception must fail the assertions, not vanish in the thread
            results[index] = ("error", repr(exc))
        finally:
            connection.close()  # each thread opened its own connection

    threads = [threading.Thread(target=run, args=(index, attempt)) for index, attempt in enumerate(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT_SECONDS)
    return results


def _status(response: Any) -> tuple[int, int]:
    """(HTTP status, envelope status code) of a response."""
    return response.status_code, response.json()["status"]


def _data(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.json()
    return response.json()["data"]


def _onboard(total_rooms: int) -> str:
    """Onboard a property with one room type of ``total_rooms`` rooms; returns the room type id."""
    client, owner = APIClient(), auth_header(OWNER, "OWNER")
    account = _data(
        client.post(
            f"{API}/catalog/owner-accounts",
            {"display_name": "Race Stays", "contact_email": "owner@race.example", "contact_phone": "9123456780"},
            **owner,
        ),
        201,
    )
    property_ = {
        "name": "Race Court",
        "property_type": "HOTEL",
        "star_rating": 3,
        "address": {"line1": "1 Mall Road", "locality": "The Ridge", "city": "Shimla", "state": "Himachal Pradesh", "postal_code": "171001"},
        "amenities": ["WIFI"],
        "pricing_strategy": "STANDARD",
        "cancellation_policy": "FLEXIBLE",
        "room_types": [{"name": "Deluxe", "max_occupancy": 2, "total_rooms": total_rooms, "base_price": "3000.00"}],
    }
    onboarded = _data(client.post(f"{API}/catalog/owner-accounts/{account['account_id']}/properties", property_, **owner), 201)
    return onboarded["room_types"][0]["room_type_id"]


def _check_in() -> date:
    return SystemClock().today() + timedelta(days=20)  # far enough ahead for a full FLEXIBLE refund


def _book_request(room_type_id: str, guest: str) -> Any:
    body = {
        "room_type_id": room_type_id,
        "check_in": _check_in().isoformat(),
        "check_out": (_check_in() + timedelta(days=2)).isoformat(),
        "guests": 2,
        **CONTACT,
    }
    return APIClient().post(f"{API}/bookings", body, **auth_header(guest, "GUEST"))


def _book(room_type_id: str, guest: str) -> str:
    booking = _data(_book_request(room_type_id, guest), 201)
    assert booking["status"] == "PENDING_PAYMENT"
    return booking["booking_id"]


def _pay(booking_id: str, guest: str, key: str) -> Any:
    return APIClient().post(
        f"{API}/bookings/{booking_id}/payments",
        {"method": "CARD", "card": CARD},
        HTTP_IDEMPOTENCY_KEY=key,
        **auth_header(guest, "GUEST"),
    )


def _cancel(booking_id: str, guest: str) -> Any:
    return APIClient().post(f"{API}/bookings/{booking_id}/cancel", {"reason": "double click"}, **auth_header(guest, "GUEST"))


def _expiry_job_running_at(now: Any, delay_seconds: float) -> Callable[[], int]:
    """The scheduler's expiry job with its clock at ``now`` (it sees the hold as lapsed), started after a delay."""
    service = ExpireBookingsService(
        DjangoBookingRepository(), InventoryAdapter(), event_bus, DjangoUnitOfWork(), FixedClock(now)
    )

    def run() -> int:
        time.sleep(delay_seconds)
        return service.execute()

    return run


def _booking(booking_id: str) -> BookingRow:
    return BookingRow.objects.get(pk=booking_id)


def _hold_status(booking_id: str) -> HoldStatus:
    return InventoryHold.objects.get(pk=_booking(booking_id).hold_id).hold_status


def _reserved_rooms(room_type_id: str) -> set[int]:
    """reserved_rooms across the stay's nights (one value when the nights agree)."""
    nights = RoomNight.objects.filter(
        room_type_id=UUID(room_type_id), stay_date__gte=_check_in(), stay_date__lt=_check_in() + timedelta(days=2)
    )
    return set(nights.values_list("reserved_rooms", flat=True))


def _payments(booking_id: str) -> list[PaymentStatus]:
    return list(PaymentRow.objects.filter(booking_id=booking_id).values_list("payment_status", flat=True))


def test_pay_racing_the_expiry_job_has_exactly_one_winner() -> None:
    """The job believes the hold lapsed while the guest pays just in time: one outcome, consistent everywhere."""
    room_type_id = _onboard(total_rooms=ROUNDS)
    confirmed = 0

    for round_ in range(ROUNDS):
        guest = f"guest-expiry-{round_}"
        booking_id = _book(room_type_id, guest)
        lapsed = timezone.now() + timedelta(seconds=settings.BOOKING_HOLD_TTL_SECONDS + 60)
        expire = _expiry_job_running_at(lapsed, JOB_DELAYS_SECONDS[round_ % len(JOB_DELAYS_SECONDS)])

        (pay_kind, pay), (job_kind, expired) = _race(
            [lambda: _pay(booking_id, guest, f"pay-expiry-{round_}"), expire]
        )

        assert (pay_kind, job_kind) == ("ok", "ok"), (pay, expired)
        status = _booking(booking_id).booking_status
        if _status(pay) == (200, SUCCESS):  # the payment locked the booking first
            assert (status, expired, _hold_status(booking_id)) == (BookingStatus.CONFIRMED, 0, HoldStatus.CONFIRMED)
            assert _payments(booking_id) == [PaymentStatus.SUCCEEDED]
            confirmed += 1
        else:  # the job expired it first; the payment then finds it EXPIRED and charges nothing
            assert _status(pay) == (409, BOOKING_EXPIRED), pay.json()
            assert (status, expired, _hold_status(booking_id)) == (BookingStatus.EXPIRED, 1, HoldStatus.RELEASED)
            assert _payments(booking_id) == []
        assert _reserved_rooms(room_type_id) == {confirmed}, f"round {round_}"


def test_double_cancel_refunds_once() -> None:
    room_type_id = _onboard(total_rooms=ROUNDS)

    for round_ in range(ROUNDS):
        guest = f"guest-cancel-{round_}"
        booking_id = _book(room_type_id, guest)
        paid = _data(_pay(booking_id, guest, f"pay-cancel-{round_}"))
        assert _reserved_rooms(room_type_id) == {1}

        results = _race([lambda: _cancel(booking_id, guest), lambda: _cancel(booking_id, guest)])

        assert [kind for kind, _ in results] == ["ok", "ok"], results
        assert sorted(_status(response) for _, response in results) == [
            (200, SUCCESS),
            (409, INVALID_STATE_TRANSITION),
        ], [response.json() for _, response in results]
        payment_id = paid["payment"]["payment_id"]
        [refund] = RefundRow.objects.filter(payment_id=payment_id)
        payment = PaymentRow.objects.get(pk=payment_id)
        assert refund.amount_paisa == payment.amount_paisa == payment.refunded_amount_paisa  # FLEXIBLE: 100%
        assert payment.payment_status == PaymentStatus.REFUNDED
        assert _booking(booking_id).booking_status == BookingStatus.CANCELLED
        assert _reserved_rooms(room_type_id) == {0}, f"round {round_}"  # released once, never below zero


def test_concurrent_pays_with_different_keys_charge_once() -> None:
    room_type_id = _onboard(total_rooms=ROUNDS)

    for round_ in range(ROUNDS):
        guest = f"guest-pay-{round_}"
        booking_id = _book(room_type_id, guest)

        results = _race([lambda key=key: _pay(booking_id, guest, key) for key in (f"pay-first-{round_}", f"pay-second-{round_}")])

        assert [kind for kind, _ in results] == ["ok", "ok"], results
        assert sorted(_status(response) for _, response in results) == [
            (200, SUCCESS),
            (409, INVALID_STATE_TRANSITION),
        ], [response.json() for _, response in results]
        assert _payments(booking_id) == [PaymentStatus.SUCCEEDED]
        assert _booking(booking_id).booking_status == BookingStatus.CONFIRMED
        assert _hold_status(booking_id) == HoldStatus.CONFIRMED
    assert _reserved_rooms(room_type_id) == {ROUNDS}


def test_last_room_booked_by_many_guests_at_once_has_one_booking() -> None:
    room_type_id = _onboard(total_rooms=1)

    results = _race([lambda n=n: _book_request(room_type_id, f"guest-last-{n}") for n in range(LAST_ROOM_GUESTS)])

    assert [kind for kind, _ in results] == ["ok"] * LAST_ROOM_GUESTS, results
    statuses = sorted(_status(response) for _, response in results)
    assert statuses == [(201, SUCCESS)] + [(409, INVENTORY_UNAVAILABLE)] * (LAST_ROOM_GUESTS - 1), statuses
    assert BookingRow.objects.filter(room_type_id=room_type_id).count() == 1
    assert InventoryHold.objects.filter(room_type_id=room_type_id).count() == 1
    assert _reserved_rooms(room_type_id) == {1}
