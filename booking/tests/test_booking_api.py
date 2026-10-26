"""Booking API over the full stack (LLD §10.2, §14): IDOR relations, PII masking and the pay contract.

Needs PostgreSQL (docker compose) with migrations applied; Redis is fakeredis. The fixtures onboard a
property and book it through the public HTTP API, so these tests depend only on the documented contract.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from django.apps import apps
from drf_spectacular.generators import SchemaGenerator
from hotel_commons.domain.clock import SystemClock
from hotel_commons.idempotency import decorators
from hotel_commons.idempotency.interfaces import BeginOutcome, IdempotencyStore, StoredResponse
from hotel_commons.models.base import RecordStatus
from redis.exceptions import RedisError
from rest_framework.test import APIClient

from booking.repositories.booking_repository import DjangoBookingRepository

pytestmark = [pytest.mark.django_db, pytest.mark.db]

API = "/hotel/v1"
OWNER = "owner-1"
GUEST = "guest-1"
GUEST_NAME = "Rahul Sharma"
GUEST_EMAIL = "rahul@example.com"
GUEST_PHONE = "9876543210"
# Payment validates expiry against the real clock: a card valid two years from now never goes stale.
CARD = {
    "card_number": "4242424242424242",
    "expiry_month": 12,
    "expiry_year": SystemClock().today().year + 2,
    "cvv": "123",
    "holder_name": GUEST_NAME,
}
DECLINED_CARD = {**CARD, "card_number": "4000000000000002"}

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


def _booking_body(room_type: str) -> dict:
    check_in = SystemClock().today() + timedelta(days=10)
    return {
        "room_type_id": room_type,
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=2)).isoformat(),
        "guests": 2,
        "guest_name": GUEST_NAME,
        "guest_email": GUEST_EMAIL,
        "guest_phone": GUEST_PHONE,
    }


class _UnreachableStore(IdempotencyStore):
    """Redis is down: ``@idempotent`` lets every request through, only Postgres can keep writes single."""

    def begin(self, key: str, fingerprint: str, lock_ttl_seconds: int) -> BeginOutcome:
        raise RedisError("connection refused")

    def complete(self, key: str, fingerprint: str, response: StoredResponse, ttl_seconds: int) -> None:
        raise RedisError("connection refused")

    def release(self, key: str) -> None:
        raise RedisError("connection refused")


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decorators, "get_idempotency_store", _UnreachableStore)


@pytest.fixture
def booked(api_client: APIClient, auth: Auth, onboarded: tuple[str, str]) -> BookedStay:
    property_id, room_type = onboarded
    booking = _ok(api_client.post(f"{API}/bookings", _booking_body(room_type), **auth(GUEST, "GUEST")), 201)
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
    payment = {"method": "CARD", "card": CARD}

    response = api_client.post(f"{API}/bookings/{booked.booking_id}/payments", payment, **auth(GUEST, "GUEST"))

    assert response.status_code == 400
    assert response.json()["status"] == 2100008  # IDEMPOTENCY_KEY_REQUIRED
    assert _ok(api_client.get(f"{API}/bookings/{booked.booking_id}", **auth(GUEST, "GUEST")))["status"] == (
        "PENDING_PAYMENT"
    )


def test_pay_confirms_and_replays_with_same_key(api_client: APIClient, auth: Auth, booked: BookedStay) -> None:
    payment = {"method": "CARD", "card": CARD}
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


@pytest.mark.usefixtures("redis_down")
def test_create_retry_returns_the_first_booking_while_redis_is_down(
    api_client: APIClient, auth: Auth, onboarded: tuple[str, str]
) -> None:
    body = _booking_body(onboarded[1])
    headers = {**auth(GUEST, "GUEST"), "HTTP_IDEMPOTENCY_KEY": "create-attempt-0001"}
    first = _ok(api_client.post(f"{API}/bookings", body, **headers), 201)

    again = api_client.post(f"{API}/bookings", body, **headers)

    assert again.status_code == 201
    assert again["Idempotent-Replayed"] == "true"
    assert again.json()["data"]["booking_id"] == first["booking_id"]
    assert _ok(api_client.get(f"{API}/bookings", **auth(GUEST, "GUEST")))["total"] == 1


@pytest.mark.usefixtures("redis_down")
def test_create_key_reused_for_another_request_is_refused_while_redis_is_down(
    api_client: APIClient, auth: Auth, onboarded: tuple[str, str]
) -> None:
    headers = {**auth(GUEST, "GUEST"), "HTTP_IDEMPOTENCY_KEY": "create-attempt-0001"}
    _ok(api_client.post(f"{API}/bookings", _booking_body(onboarded[1]), **headers), 201)

    response = api_client.post(f"{API}/bookings", {**_booking_body(onboarded[1]), "guests": 1}, **headers)

    assert response.status_code == 422
    assert response.json()["status"] == 2100011  # IDEMPOTENCY_KEY_REUSED


def test_create_response_shows_the_stored_creation_time(
    api_client: APIClient, auth: Auth, onboarded: tuple[str, str]
) -> None:
    created = _ok(api_client.post(f"{API}/bookings", _booking_body(onboarded[1]), **auth(GUEST, "GUEST")), 201)

    stored = _ok(api_client.get(f"{API}/bookings/{created['booking_id']}", **auth(GUEST, "GUEST")))

    assert created["created_at"] == stored["created_at"]


@pytest.mark.usefixtures("redis_down")
def test_pay_key_reused_for_another_method_is_refused_while_redis_is_down(
    api_client: APIClient, auth: Auth, booked: BookedStay
) -> None:
    url = f"{API}/bookings/{booked.booking_id}/payments"
    headers = {**auth(GUEST, "GUEST"), "HTTP_IDEMPOTENCY_KEY": "pay-attempt-0001"}
    assert api_client.post(url, {"method": "CARD", "card": DECLINED_CARD}, **headers).status_code == 402

    response = api_client.post(url, {"method": "UPI", "upi": {"vpa": "rahul@okaxis"}}, **headers)

    assert response.status_code == 422
    assert response.json()["status"] == 2100011  # IDEMPOTENCY_KEY_REUSED, not a replay of the decline
    assert _ok(api_client.get(f"{API}/bookings/{booked.booking_id}", **auth(GUEST, "GUEST")))["status"] == (
        "PENDING_PAYMENT"
    )


def test_deactivated_owner_account_loses_access_to_its_bookings(
    api_client: APIClient, auth: Auth, booked: BookedStay
) -> None:
    detail = f"{API}/bookings/{booked.booking_id}"
    assert api_client.get(detail, **auth(OWNER, "OWNER")).status_code == 200
    owner_accounts = apps.get_model("catalog", "OwnerAccount").objects  # deactivated out of band (no endpoint)
    owner_accounts.filter(owner_user_id=OWNER).update(status=RecordStatus.INACTIVE)

    response = api_client.get(detail, **auth(OWNER, "OWNER"))

    assert response.status_code == 404
    assert _ok(api_client.get(detail, **auth(GUEST, "GUEST")))["booking_id"] == booked.booking_id


@pytest.fixture(scope="module")
def schema() -> dict:
    return SchemaGenerator().get_schema(request=None, public=True)


def _header_parameters(operation: dict) -> dict[str, bool]:
    return {p["name"]: p.get("required", False) for p in operation["parameters"] if p["in"] == "header"}


def test_schema_documents_the_idempotency_key_header(schema: dict) -> None:
    paths = schema["paths"]

    assert _header_parameters(paths[f"{API}/bookings/{{booking_id}}/payments"]["post"]) == {"Idempotency-Key": True}
    assert _header_parameters(paths[f"{API}/bookings"]["post"]) == {"Idempotency-Key": False}
    assert _header_parameters(paths[f"{API}/bookings/{{booking_id}}/cancel"]["post"]) == {"Idempotency-Key": False}
    assert "Idempotent-Replayed" in paths[f"{API}/bookings"]["post"]["responses"]["201"]["headers"]


def _component(schema: dict, ref_holder: dict) -> dict:
    return schema["components"]["schemas"][ref_holder["$ref"].rsplit("/", 1)[1]]


def test_schema_documents_the_list_as_one_envelope(schema: dict) -> None:
    response = schema["paths"][f"{API}/bookings"]["get"]["responses"]["200"]["content"]["application/json"]

    envelope = _component(schema, response["schema"])
    page = _component(schema, envelope["properties"]["data"])
    assert envelope["type"] == "object"
    assert page["properties"]["items"] == {"type": "array", "items": {"$ref": "#/components/schemas/Booking"}}


@pytest.mark.parametrize(
    ("component", "fields"),
    [
        (
            "Booking",
            ["hold_expires_at", "payment_id", "last_payment_failure", "confirmed_at", "cancelled_at", "expired_at"],
        ),
        ("PaymentSummary", ["failure_code", "failure_message"]),
    ],
)
def test_schema_marks_null_fields_nullable(schema: dict, component: str, fields: list[str]) -> None:
    properties = schema["components"]["schemas"][component]["properties"]

    assert {name: properties[name].get("nullable", False) for name in fields} == dict.fromkeys(fields, True)


def test_schema_documents_both_shapes_of_a_pay_conflict(schema: dict) -> None:
    conflict = schema["paths"][f"{API}/bookings/{{booking_id}}/payments"]["post"]["responses"]["409"]

    assert _component(schema, conflict["content"]["application/json"]["schema"])["oneOf"] == [
        {"$ref": "#/components/schemas/BookingPaymentEnvelope"},
        {"$ref": "#/components/schemas/ErrorEnvelope"},
    ]


def test_expiry_scan_leaves_out_the_bookings_a_run_already_tried(
    api_client: APIClient, auth: Auth, onboarded: tuple[str, str]
) -> None:
    ids = [
        _ok(api_client.post(f"{API}/bookings", _booking_body(onboarded[1]), **auth(GUEST, "GUEST")), 201)["booking_id"]
        for _ in range(2)
    ]
    after_expiry = SystemClock().now() + timedelta(hours=1)
    bookings = DjangoBookingRepository()

    assert [str(i) for i in bookings.find_expired_pending_ids(after_expiry, 10)] == ids  # oldest expiry first
    assert [str(i) for i in bookings.find_expired_pending_ids(after_expiry, 10, exclude={UUID(ids[0])})] == ids[1:]
