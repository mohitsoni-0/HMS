"""The brief's booking edge cases over HTTP: status, envelope code and envelope shape (LLD §10.2, §12 errors).

The booking services test these rules on in-memory fakes; this module proves each one reaches the client as
the documented HTTP status and service status code, in the error envelope ``{status, message, errors, data,
request_id}`` with ``data`` null. Needs PostgreSQL with migrations applied; Redis is fakeredis.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from django.conf import settings
from hotel_commons.domain.clock import SystemClock
from hotel_commons.testing.tokens import auth_header
from rest_framework.test import APIClient

from booking.models.booking_models import Booking as BookingRow

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

API = "/hotel/v1"
OWNER = "owner-edge"
GUEST = "guest-edge"
OTHER_GUEST = "guest-edge-2"
CONTACT = {"guest_name": "Rahul Sharma", "guest_email": "rahul@example.com", "guest_phone": "9876543210"}
CARD = {
    "card_number": "4242424242424242",
    "expiry_month": 12,
    "expiry_year": SystemClock().today().year + 2,  # validated against the real clock, so never hard-coded
    "cvv": "123",
    "holder_name": "Rahul Sharma",
}
ENVELOPE_KEYS = {"status", "message", "errors", "data", "request_id"}

# Service status codes (LLD §3.2 and each context's status_code.py)
VALIDATION_FAILED = 2100001
UNAUTHENTICATED = 2100002
IDEMPOTENCY_KEY_REUSED = 2100011
INVENTORY_UNAVAILABLE = 2102001
INVALID_STATE_TRANSITION = 2105002
INVALID_BOOKING_REQUEST = 2105005
CANCELLATION_WINDOW_CLOSED = 2105007
GUEST_CAPACITY_EXCEEDED = 2105008
BOOKING_NOT_PAYABLE = 2105009
BOOKING_NOT_FOUND = 2105001
RESOURCE_NOT_FOUND = 2100006
INVALID_PAYMENT_DETAILS = 2106002


@dataclass(frozen=True)
class Hotel:
    client: APIClient
    deluxe_id: str  # one room, two guests at most
    check_in: date

    def book_body(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "room_type_id": self.deluxe_id,
            "check_in": self.check_in.isoformat(),
            "check_out": (self.check_in + timedelta(days=2)).isoformat(),
            "guests": 2,
            **CONTACT,
        }
        return {**body, **overrides}

    def post_booking(self, guest: str = GUEST, **overrides: Any) -> Any:
        return self.client.post(f"{API}/bookings", self.book_body(**overrides), **auth_header(guest, "GUEST"))

    def book(self, **overrides: Any) -> str:
        response = self.post_booking(**overrides)
        assert response.status_code == 201, response.json()
        return response.json()["data"]["booking_id"]

    def pay(self, booking_id: str, key: str, card: dict[str, Any] = CARD, guest: str = GUEST) -> Any:
        return self.client.post(
            f"{API}/bookings/{booking_id}/payments",
            {"method": "CARD", "card": card},
            HTTP_IDEMPOTENCY_KEY=key,
            **auth_header(guest, "GUEST"),
        )

    def cancel(self, booking_id: str) -> Any:
        return self.client.post(f"{API}/bookings/{booking_id}/cancel", {"reason": "edge"}, **auth_header(GUEST, "GUEST"))

    def confirmed_booking(self) -> str:
        booking_id = self.book()
        assert self.pay(booking_id, "pay-edge-first").status_code == 200
        return booking_id


@pytest.fixture
def hotel(api_client: APIClient) -> Hotel:
    owner = auth_header(OWNER, "OWNER")
    account = api_client.post(
        f"{API}/catalog/owner-accounts",
        {"display_name": "Edge Stays", "contact_email": "owner@edge.example", "contact_phone": "9123456780"},
        **owner,
    ).json()["data"]
    property_ = {
        "name": "Edge Inn",
        "property_type": "HOTEL",
        "star_rating": 3,
        "address": {
            "line1": "4 Lake Road",
            "locality": "Mallital",
            "city": "Nainital",
            "state": "Uttarakhand",
            "postal_code": "263001",
        },
        "amenities": ["WIFI"],
        "pricing_strategy": "STANDARD",
        "cancellation_policy": "FLEXIBLE",
        "room_types": [{"name": "Deluxe", "max_occupancy": 2, "total_rooms": 1, "base_price": "3500.00"}],
    }
    response = api_client.post(f"{API}/catalog/owner-accounts/{account['account_id']}/properties", property_, **owner)
    assert response.status_code == 201, response.json()
    deluxe_id = response.json()["data"]["room_types"][0]["room_type_id"]
    return Hotel(api_client, deluxe_id, SystemClock().today() + timedelta(days=14))


def _sold_out(hotel: Hotel) -> Any:
    hotel.book()
    return hotel.post_booking(guest=OTHER_GUEST)


def _over_capacity(hotel: Hotel) -> Any:
    return hotel.post_booking(guests=3)


def _check_out_on_check_in(hotel: Hotel) -> Any:
    return hotel.post_booking(check_out=hotel.check_in.isoformat())


def _check_out_before_check_in(hotel: Hotel) -> Any:
    return hotel.post_booking(check_out=(hotel.check_in - timedelta(days=1)).isoformat())


def _stay_too_long(hotel: Hotel) -> Any:
    return hotel.post_booking(check_out=(hotel.check_in + timedelta(days=settings.MAX_STAY_NIGHTS + 1)).isoformat())


def _check_in_in_the_past(hotel: Hotel) -> Any:
    yesterday = SystemClock().today() - timedelta(days=1)
    return hotel.post_booking(check_in=yesterday.isoformat(), check_out=(yesterday + timedelta(days=2)).isoformat())


def _beyond_the_horizon(hotel: Hotel) -> Any:
    check_in = SystemClock().today() + timedelta(days=settings.INVENTORY_HORIZON_DAYS)
    return hotel.post_booking(check_in=check_in.isoformat(), check_out=(check_in + timedelta(days=1)).isoformat())


def _missing_contact(hotel: Hotel) -> Any:
    body = hotel.book_body()
    del body["guest_email"]
    return hotel.client.post(f"{API}/bookings", body, **auth_header(GUEST, "GUEST"))


def _without_token(hotel: Hotel) -> Any:
    return hotel.client.post(f"{API}/bookings", hotel.book_body())


def _pay_confirmed(hotel: Hotel) -> Any:
    return hotel.pay(hotel.confirmed_booking(), "pay-edge-second")


def _pay_cancelled(hotel: Hotel) -> Any:
    booking_id = hotel.book()
    assert hotel.cancel(booking_id).status_code == 200
    return hotel.pay(booking_id, "pay-edge-after-cancel")


def _double_cancel(hotel: Hotel) -> Any:
    booking_id = hotel.confirmed_booking()
    assert hotel.cancel(booking_id).status_code == 200
    return hotel.cancel(booking_id)


def _pay_key_reused_with_another_card(hotel: Hotel) -> Any:
    booking_id = hotel.book()
    declined = {**CARD, "card_number": "4000000000000002"}  # the mock gateway declines cards ending 0002
    assert hotel.pay(booking_id, "pay-edge-reused", card=declined).status_code == 402
    return hotel.pay(booking_id, "pay-edge-reused")


def _invalid_card_number(hotel: Hotel) -> Any:
    return hotel.pay(hotel.book(), "pay-edge-luhn", card={**CARD, "card_number": "4242424242424241"})


def _cancel_after_check_in(hotel: Hotel) -> Any:
    booking_id = hotel.confirmed_booking()
    started = SystemClock().today() - timedelta(days=1)
    BookingRow.objects.filter(pk=booking_id).update(check_in=started, check_out=started + timedelta(days=2))
    return hotel.cancel(booking_id)


def _pay_a_pay_at_property_booking(hotel: Hotel) -> Any:
    return hotel.pay(hotel.book(booking_type="PAY_AT_PROPERTY"), "pay-edge-at-property")


def _pay_someone_elses_booking(hotel: Hotel) -> Any:
    return hotel.pay(hotel.book(), "pay-edge-stranger", guest=OTHER_GUEST)


# scenario -> (HTTP status, service status code, field expected in "errors" or None)
SCENARIOS: dict[str, tuple[Callable[[Hotel], Any], int, int, str | None]] = {
    "sold-out room type": (_sold_out, 409, INVENTORY_UNAVAILABLE, None),
    "guests over capacity": (_over_capacity, 422, GUEST_CAPACITY_EXCEEDED, None),
    "check_out on check_in": (_check_out_on_check_in, 400, VALIDATION_FAILED, "check_out"),
    "check_out before check_in": (_check_out_before_check_in, 400, VALIDATION_FAILED, "check_out"),
    "stay longer than the maximum": (_stay_too_long, 400, VALIDATION_FAILED, "check_out"),
    "check_in in the past": (_check_in_in_the_past, 400, INVALID_BOOKING_REQUEST, None),
    "stay beyond the booking horizon": (_beyond_the_horizon, 400, INVALID_BOOKING_REQUEST, None),
    "missing guest contact": (_missing_contact, 400, VALIDATION_FAILED, "guest_email"),
    "no bearer token": (_without_token, 401, UNAUTHENTICATED, None),
    "pay a CONFIRMED booking": (_pay_confirmed, 409, INVALID_STATE_TRANSITION, None),
    "pay a CANCELLED booking": (_pay_cancelled, 409, INVALID_STATE_TRANSITION, None),
    "cancel twice": (_double_cancel, 409, INVALID_STATE_TRANSITION, None),
    "pay key reused with another body": (_pay_key_reused_with_another_card, 422, IDEMPOTENCY_KEY_REUSED, None),
    "card number fails the Luhn check": (_invalid_card_number, 400, INVALID_PAYMENT_DETAILS, "card_number"),
    "cancel after check-in": (_cancel_after_check_in, 422, CANCELLATION_WINDOW_CLOSED, None),
    "pay a pay-at-property booking": (_pay_a_pay_at_property_booking, 422, BOOKING_NOT_PAYABLE, None),
    "pay another guest's booking": (_pay_someone_elses_booking, 404, RESOURCE_NOT_FOUND, None),
}


@pytest.mark.parametrize(("scenario", "http_status", "code", "error_field"), [
    (name, http_status, code, field) for name, (_, http_status, code, field) in SCENARIOS.items()
], ids=list(SCENARIOS))
def test_edge_case_maps_to_status_and_error_envelope(
    hotel: Hotel, scenario: str, http_status: int, code: int, error_field: str | None
) -> None:
    response = SCENARIOS[scenario][0](hotel)

    body = response.json()
    assert (response.status_code, body["status"]) == (http_status, code), body
    assert set(body) == ENVELOPE_KEYS, body
    assert body["data"] is None and body["message"], body
    if error_field is None:
        assert body["errors"] is None, body
    else:
        assert _names_field(body["errors"], error_field), body


def _names_field(errors: Any, field: str) -> bool:
    """Whether the errors map names ``field``: as a key, as the leaf of a dotted path, or in a nested map."""
    if not isinstance(errors, dict):
        return False
    return any(
        key == field or key.endswith(f".{field}") or _names_field(value, field) for key, value in errors.items()
    )


def test_sold_out_leaves_the_first_booking_alone(hotel: Hotel) -> None:
    _sold_out(hotel)

    [row] = BookingRow.objects.filter(room_type_id=hotel.deluxe_id)
    assert row.guest_id == GUEST
