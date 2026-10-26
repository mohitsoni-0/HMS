"""Booking API over the full stack (LLD §10.2, §14): IDOR relations, PII masking and the pay contract.

Needs PostgreSQL (docker compose) with migrations applied; Redis is fakeredis. The fixtures onboard a
property and book it through the public HTTP API, so these tests depend only on the documented contract.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest
from hotel_commons.domain.clock import SystemClock
from rest_framework.test import APIClient

pytestmark = [pytest.mark.django_db, pytest.mark.db]

API = "/hotel/v1"
OWNER = "owner-1"
GUEST = "guest-1"
GUEST_NAME = "Rahul Sharma"
GUEST_EMAIL = "rahul@example.com"
GUEST_PHONE = "9876543210"

Auth = Callable[..., dict[str, str]]


@dataclass(frozen=True)
class BookedStay:
    booking_id: str
    property_id: str


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.json()
    return response.json()["data"]


@pytest.fixture
def onboarded(api_client: APIClient, auth: Auth) -> tuple[str, str]:
    """A Goa hotel with one room type, onboarded by OWNER; returns (property_id, room_type_id)."""
    owner = auth(OWNER, "OWNER")
    account = _ok(
        api_client.post(
            f"{API}/catalog/owner-accounts",
            {"display_name": "Sea Breeze Hotels", "contact_email": "owner@example.com", "contact_phone": "9123456780"},
            **owner,
        ),
        201,
    )
    property_ = _ok(
        api_client.post(
            f"{API}/catalog/owner-accounts/{account['account_id']}/properties",
            {
                "name": "Sea Breeze Resort",
                "property_type": "HOTEL",
                "star_rating": 4,
                "address": {
                    "line1": "1 Beach Road",
                    "locality": "Candolim",
                    "city": "Goa",
                    "state": "Goa",
                    "postal_code": "403515",
                },
                "amenities": ["WIFI"],
                "pricing_strategy": "STANDARD",
                "cancellation_policy": "FLEXIBLE",
                "room_types": [{"name": "Deluxe", "max_occupancy": 2, "total_rooms": 3, "base_price": "4000.00"}],
            },
            **owner,
        ),
        201,
    )
    return property_["property_id"], property_["room_types"][0]["room_type_id"]


@pytest.fixture
def booked(api_client: APIClient, auth: Auth, onboarded: tuple[str, str]) -> BookedStay:
    property_id, room_type = onboarded
    check_in = SystemClock().today() + timedelta(days=10)
    booking = _ok(
        api_client.post(
            f"{API}/bookings",
            {
                "room_type_id": room_type,
                "check_in": check_in.isoformat(),
                "check_out": (check_in + timedelta(days=2)).isoformat(),
                "guests": 2,
                "guest_name": GUEST_NAME,
                "guest_email": GUEST_EMAIL,
                "guest_phone": GUEST_PHONE,
            },
            **auth(GUEST, "GUEST"),
        ),
        201,
    )
    assert booking["status"] == "PENDING_PAYMENT"
    assert booking["total"] == "8000.00"
    return BookedStay(booking_id=booking["booking_id"], property_id=property_id)


def test_dual_role_owner_cannot_cancel_strangers_booking(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    owner_and_guest = auth(OWNER, "GUEST", "OWNER")
    detail = f"{API}/bookings/{booked.booking_id}"

    assert _ok(api_client.get(detail, **owner_and_guest))["booking_id"] == booked.booking_id  # owner may read

    response = api_client.post(f"{detail}/cancel", {"reason": "not mine"}, **owner_and_guest)

    assert response.status_code == 404
    assert response.json()["data"] is None
    assert _ok(api_client.get(detail, **auth(GUEST, "GUEST")))["status"] == "PENDING_PAYMENT"


def test_owner_sees_masked_guest_pii(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    owner = auth(OWNER, "OWNER")

    booking = _ok(api_client.get(f"{API}/bookings/{booked.booking_id}", **owner))
    [listed] = _ok(api_client.get(f"{API}/bookings", {"property_id": booked.property_id}, **owner))["items"]

    for view in (booking, listed):
        assert view["guest_name"] == "R**** S*****"
        assert view["guest_email"] == "r****@example.com"
        assert view["guest_phone"] == "******3210"


def test_guest_sees_own_pii(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    booking = _ok(api_client.get(f"{API}/bookings/{booked.booking_id}", **auth(GUEST, "GUEST")))

    assert (booking["guest_name"], booking["guest_email"], booking["guest_phone"]) == (
        GUEST_NAME,
        GUEST_EMAIL,
        GUEST_PHONE,
    )


def test_other_guest_gets_404(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    response = api_client.get(f"{API}/bookings/{booked.booking_id}", **auth("guest-2", "GUEST"))

    assert response.status_code == 404


def test_pay_requires_idempotency_key(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    card = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2030, "cvv": "123"}
    payment = {"method": "CARD", "card": {**card, "holder_name": GUEST_NAME}}

    response = api_client.post(f"{API}/bookings/{booked.booking_id}/payments", payment, **auth(GUEST, "GUEST"))

    assert response.status_code == 400
    assert response.json()["status"] == 2100008  # IDEMPOTENCY_KEY_REQUIRED
    assert _ok(api_client.get(f"{API}/bookings/{booked.booking_id}", **auth(GUEST, "GUEST")))["status"] == (
        "PENDING_PAYMENT"
    )


def test_pay_confirms_and_replays_with_same_key(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    card = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2030, "cvv": "123"}
    payment = {"method": "CARD", "card": {**card, "holder_name": GUEST_NAME}}
    url = f"{API}/bookings/{booked.booking_id}/payments"
    headers = {**auth(GUEST, "GUEST"), "HTTP_IDEMPOTENCY_KEY": "pay-attempt-0001"}

    first = _ok(api_client.post(url, payment, **headers))
    again = api_client.post(url, payment, **headers)

    assert first["booking"]["status"] == "CONFIRMED"
    assert first["payment"]["status"] == "SUCCEEDED"
    assert first["payment"]["instrument"] == "VISA •••• 4242"
    assert again.status_code == 200
    assert again.json()["data"]["payment"]["payment_id"] == first["payment"]["payment_id"]


def test_owner_must_filter_list_by_property(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    response = api_client.get(f"{API}/bookings", **auth(OWNER, "OWNER"))

    assert response.status_code == 400
    assert response.json()["status"] == 2105011  # LIST_FILTER_REQUIRED


def test_only_admin_may_book_on_behalf_of_someone(
    api_client: APIClient, auth: Auth, onboarded: tuple[str, str]
) -> None:
    check_in = SystemClock().today() + timedelta(days=10)
    body = {
        "room_type_id": onboarded[1],
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=1)).isoformat(),
        "guests": 1,
        "guest_name": GUEST_NAME,
        "guest_email": GUEST_EMAIL,
        "guest_phone": GUEST_PHONE,
        "on_behalf_of_user_id": "guest-9",
    }

    response = api_client.post(f"{API}/bookings", body, **auth(GUEST, "GUEST"))

    assert response.status_code == 403
    assert response.json()["status"] == 2100005  # FIELD_ACCESS_DENIED
