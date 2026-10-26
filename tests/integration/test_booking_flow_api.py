"""End-to-end booking flows over HTTP (LLD §10.2, §10.4, §14 "API end-to-end").

Every step goes through the public API with signed bearer tokens, against PostgreSQL with migrations applied
(Redis is fakeredis). An owner opens an account and onboards a property with two room types; a guest finds it
in search with its nightly prices, books, pays (the retry with the same Idempotency-Key is replayed), has a
second booking's card declined, cancels with a refund by the property's policy, and search shows the room
again. Other flows check PII masking and IDOR, and a lapsed hold expired by the scheduler's job.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone
from hotel_commons.domain.clock import SystemClock
from hotel_commons.testing.tokens import auth_header
from rest_framework.test import APIClient

from booking.models.booking_models import Booking as BookingRow
from payment.models.payment_models import Payment as PaymentRow

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

API = "/hotel/v1"
OWNER = "owner-e2e"
GUEST = "guest-e2e"
STRANGER = "guest-stranger"
THURSDAY = 3

CONTACT = {"guest_name": "Rahul Sharma", "guest_email": "rahul@example.com", "guest_phone": "9876543210"}
MASKED_CONTACT = {"guest_name": "R**** S*****", "guest_email": "r****@example.com", "guest_phone": "******3210"}
CARD = {
    "card_number": "4242424242424242",
    "expiry_month": 12,
    "expiry_year": 2030,
    "cvv": "123",
    "holder_name": "Rahul Sharma",
}
DECLINED_CARD = {**CARD, "card_number": "4000000000000002"}  # the mock gateway declines cards ending 0002

# WEEKEND pricing: Friday and Saturday nights at the base price x 1.20. STRICT refunds 50% a week or more ahead.
PROPERTY = {
    "name": "Courtyard Heritage Hotel",
    "property_type": "HOTEL",
    "star_rating": 4,
    "address": {
        "line1": "12 Rue Romain Rolland",
        "locality": "White Town",
        "city": "Puducherry",
        "state": "Puducherry",
        "postal_code": "605001",
    },
    "amenities": ["WIFI", "POOL"],
    "pricing_strategy": "WEEKEND",
    "cancellation_policy": "STRICT",
    "room_types": [
        {"name": "Deluxe", "max_occupancy": 2, "total_rooms": 1, "base_price": "4000.00"},
        {"name": "Suite", "max_occupancy": 4, "total_rooms": 2, "base_price": "7500.50", "amenities": ["BALCONY"]},
    ],
}


@dataclass(frozen=True)
class Hotel:
    property_id: str
    deluxe_id: str  # the only Deluxe room
    suite_id: str  # one of two suites


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date

    @property
    def nights(self) -> list[str]:
        return [(self.check_in + timedelta(days=n)).isoformat() for n in range((self.check_out - self.check_in).days)]


def _data(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.json()
    return response.json()["data"]


@pytest.fixture
def hotel(api_client: APIClient) -> Hotel:
    """The owner opens an account and onboards one property with two room types."""
    owner = auth_header(OWNER, "OWNER")
    account = _data(
        api_client.post(
            f"{API}/catalog/owner-accounts",
            {"display_name": "Heritage Stays", "contact_email": "owner@heritage.example", "contact_phone": "9123456780"},
            **owner,
        ),
        201,
    )
    assert account["owner_user_id"] == OWNER
    onboarded = _data(
        api_client.post(f"{API}/catalog/owner-accounts/{account['account_id']}/properties", PROPERTY, **owner), 201
    )
    room_types = {room_type["name"]: room_type["room_type_id"] for room_type in onboarded["room_types"]}
    return Hotel(onboarded["property_id"], room_types["Deluxe"], room_types["Suite"])


@pytest.fixture
def stay() -> Stay:
    """Thursday to Sunday, at least three weeks ahead: a weekday night, then Friday and Saturday."""
    earliest = SystemClock().today() + timedelta(days=21)
    check_in = earliest + timedelta(days=(THURSDAY - earliest.weekday()) % 7)
    return Stay(check_in, check_in + timedelta(days=3))


def _offers(client: APIClient, stay: Stay, property_id: str) -> dict[str, dict[str, Any]]:
    """Room type name -> offer of the property in a public search for two guests; {} when it is not listed."""
    page = _data(
        client.get(
            f"{API}/discovery/search",
            {"city": "puducherry", "check_in": stay.check_in.isoformat(), "check_out": stay.check_out.isoformat(),
             "guests": 2},
        )
    )
    results = [item for item in page["items"] if item["property_id"] == property_id]
    return {offer["room_type"]: offer for result in results for offer in result["offers"]}


def _book(client: APIClient, room_type_id: str, stay: Stay, key: str) -> dict[str, Any]:
    body = {
        "room_type_id": room_type_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "guests": 2,
        **CONTACT,
    }
    booking = _data(client.post(f"{API}/bookings", body, HTTP_IDEMPOTENCY_KEY=key, **auth_header(GUEST, "GUEST")), 201)
    assert booking["status"] == "PENDING_PAYMENT"
    return booking


def _rates(stay: Stay, *prices: str) -> list[dict[str, str]]:
    return [{"date": night, "price": price} for night, price in zip(stay.nights, prices, strict=True)]


def test_onboard_search_book_pay_cancel_search(api_client: APIClient, hotel: Hotel, stay: Stay) -> None:
    guest = auth_header(GUEST, "GUEST")

    # Search lists both room types with their nightly prices: Friday and Saturday cost 20% more.
    offers = _offers(api_client, stay, hotel.property_id)
    assert offers["Deluxe"]["nightly_rates"] == _rates(stay, "4000.00", "4800.00", "4800.00")
    assert (offers["Deluxe"]["total"], offers["Deluxe"]["average_nightly"]) == ("13600.00", "4533.33")
    assert offers["Suite"]["nightly_rates"] == _rates(stay, "7500.50", "9000.60", "9000.60")
    assert (offers["Suite"]["total"], offers["Suite"]["average_nightly"]) == ("25501.70", "8500.57")
    assert (offers["Deluxe"]["available_rooms"], offers["Suite"]["available_rooms"]) == (1, 2)
    assert {offer["pricing_strategy"] for offer in offers.values()} == {"WEEKEND"}

    # Book the only Deluxe room: the hold takes it out of search.
    booking = _book(api_client, hotel.deluxe_id, stay, key="book-deluxe-0001")
    assert (booking["total"], booking["nights"], booking["cancellation_policy"]) == ("13600.00", 3, "STRICT")
    assert booking["nightly_rates"] == _rates(stay, "4000.00", "4800.00", "4800.00")
    assert booking["hold_expires_at"] is not None
    assert "Deluxe" not in _offers(api_client, stay, hotel.property_id)

    # Pay by card, then send the same request again: the first response is replayed, nothing is charged twice.
    pay_url = f"{API}/bookings/{booking['booking_id']}/payments"
    pay = {"method": "CARD", "card": CARD}
    paid = api_client.post(pay_url, pay, HTTP_IDEMPOTENCY_KEY="pay-deluxe-0001", HTTP_X_REQUEST_ID="pay-1", **guest)
    retry = api_client.post(pay_url, pay, HTTP_IDEMPOTENCY_KEY="pay-deluxe-0001", HTTP_X_REQUEST_ID="pay-2", **guest)
    assert paid.status_code == 200 and "Idempotent-Replayed" not in paid.headers
    assert (retry.status_code, retry.headers["Idempotent-Replayed"]) == (200, "true")
    assert retry.json() == {**paid.json(), "request_id": "pay-2"}  # same body; the request id is the retry's own
    payment = paid.json()["data"]["payment"]
    assert paid.json()["data"]["booking"]["status"] == "CONFIRMED"
    assert (payment["status"], payment["instrument"]) == ("SUCCEEDED", "VISA •••• 4242")
    assert PaymentRow.objects.filter(booking_id=booking["booking_id"]).count() == 1

    # A second booking paid with a card the gateway declines: 402, and it stays payable.
    suite = _book(api_client, hotel.suite_id, stay, key="book-suite-0001")
    declined = api_client.post(
        f"{API}/bookings/{suite['booking_id']}/payments",
        {"method": "CARD", "card": DECLINED_CARD},
        HTTP_IDEMPOTENCY_KEY="pay-suite-0001",
        **guest,
    )
    assert declined.status_code == 402
    assert declined.json()["status"] == 2105004  # PAYMENT_DECLINED
    declined_data = declined.json()["data"]
    assert (declined_data["payment"]["status"], declined_data["payment"]["failure_code"]) == ("FAILED", "card_declined")
    assert declined_data["booking"]["status"] == "PENDING_PAYMENT"
    assert _data(api_client.get(f"{API}/bookings/{suite['booking_id']}", **guest))["status"] == "PENDING_PAYMENT"

    # Cancel the paid booking three weeks ahead: STRICT refunds half.
    cancelled = _data(
        api_client.post(f"{API}/bookings/{booking['booking_id']}/cancel", {"reason": "plans changed"}, **guest)
    )
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["refund"] == {"amount": "6800.00", "percent": "50.00", "status": "REFUNDED"}
    refunded = _data(api_client.get(f"{API}/payments/{payment['payment_id']}", **guest))
    assert (refunded["status"], refunded["refunded_amount"]) == ("PARTIALLY_REFUNDED", "6800.00")

    # Search shows the Deluxe room again; the declined Suite booking still holds one of the two suites.
    offers = _offers(api_client, stay, hotel.property_id)
    assert (offers["Deluxe"]["available_rooms"], offers["Suite"]["available_rooms"]) == (1, 1)


def test_room_type_added_after_onboarding_is_searchable_and_bookable(
    api_client: APIClient, hotel: Hotel, stay: Stay
) -> None:
    added = _data(
        api_client.post(
            f"{API}/catalog/properties/{hotel.property_id}/room-types",
            {"name": "Garden Room", "max_occupancy": 2, "total_rooms": 3, "base_price": "2500.00"},
            **auth_header(OWNER, "OWNER"),
        ),
        201,
    )

    # RoomTypeAdded re-projects the listing, and the new room type's nights were provisioned with it.
    garden = _offers(api_client, stay, hotel.property_id)["Garden Room"]
    assert garden["nightly_rates"] == _rates(stay, "2500.00", "3000.00", "3000.00")
    assert garden["available_rooms"] == 3
    booking = _book(api_client, added["room_type_id"], stay, key="book-garden-0001")
    assert booking["total"] == "8500.00"
    assert _offers(api_client, stay, hotel.property_id)["Garden Room"]["available_rooms"] == 2


def test_owner_sees_masked_guest_pii_and_other_guests_get_404(api_client: APIClient, hotel: Hotel, stay: Stay) -> None:
    booking = _book(api_client, hotel.deluxe_id, stay, key="book-pii-0001")
    detail = f"{API}/bookings/{booking['booking_id']}"
    owner = auth_header(OWNER, "OWNER")

    as_owner = _data(api_client.get(detail, **owner))
    [listed] = _data(api_client.get(f"{API}/bookings", {"property_id": hotel.property_id}, **owner))["items"]
    as_guest = _data(api_client.get(detail, **auth_header(GUEST, "GUEST")))
    stranger = api_client.get(detail, **auth_header(STRANGER, "GUEST"))
    other_owner = api_client.get(f"{API}/bookings", {"property_id": hotel.property_id}, **auth_header("owner-x", "OWNER"))

    for view in (as_owner, listed):
        assert {field: view[field] for field in MASKED_CONTACT} == MASKED_CONTACT
    assert {field: as_guest[field] for field in CONTACT} == CONTACT
    for response in (stranger, other_owner):
        assert response.status_code == 404
        assert (response.json()["status"], response.json()["data"]) == (2100006, None)  # RESOURCE_NOT_FOUND
    assert _data(api_client.get(f"{API}/bookings", **auth_header(STRANGER, "GUEST")))["total"] == 0


def test_lapsed_hold_is_expired_by_the_job_and_the_room_is_bookable_again(
    api_client: APIClient, hotel: Hotel, stay: Stay
) -> None:
    guest = auth_header(GUEST, "GUEST")
    booking = _book(api_client, hotel.deluxe_id, stay, key="book-expire-0001")
    assert "Deluxe" not in _offers(api_client, stay, hotel.property_id)

    BookingRow.objects.filter(pk=booking["booking_id"]).update(hold_expires_at=timezone.now() - timedelta(minutes=1))
    output = StringIO()
    call_command("expire_pending_bookings", stdout=output)

    assert output.getvalue().strip() == "Expired 1 booking(s)"
    expired = _data(api_client.get(f"{API}/bookings/{booking['booking_id']}", **guest))
    assert expired["status"] == "EXPIRED" and expired["expired_at"] is not None
    late = api_client.post(
        f"{API}/bookings/{booking['booking_id']}/payments",
        {"method": "CARD", "card": CARD},
        HTTP_IDEMPOTENCY_KEY="pay-late-0001",
        **guest,
    )
    assert late.status_code == 409
    assert late.json()["status"] == 2105003  # BOOKING_EXPIRED
    assert (late.json()["data"]["booking"]["status"], late.json()["data"]["payment"]) == ("EXPIRED", None)
    assert _offers(api_client, stay, hotel.property_id)["Deluxe"]["available_rooms"] == 1
    _book(api_client, hotel.deluxe_id, stay, key="book-expire-0002")
