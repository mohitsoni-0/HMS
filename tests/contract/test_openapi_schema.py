"""The published OpenAPI document is valid and tells the truth about real responses (LLD §3.9, §10, §14).

- The schema drf-spectacular generates passes its validator with no generator warnings or errors (what
  ``manage.py spectacular --validate --fail-on-warn`` checks), and ``/schema`` serves it.
- The write operations declare ``Idempotency-Key`` (required on pay, optional on the others).
- A booking journey over HTTP, errors included, is checked response by response against the documented
  schema for that path, method and status: an undocumented status, a missing required field, a null in a
  non-nullable field or an array where an envelope object is sent all fail. OpenAPI 3.0 ``nullable`` is
  read as "this type or null".

Needs PostgreSQL with migrations applied; Redis is fakeredis.
"""

import re
import uuid
from collections.abc import Iterator
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from hotel_commons.domain.clock import SystemClock
from hotel_commons.testing.tokens import auth_header
from jsonschema import Draft202012Validator
from rest_framework.test import APIClient

from booking.models.booking_models import Booking as BookingRow

API = "/hotel/v1"
OWNER = "owner-contract"
GUEST = "guest-contract"
CARD = {
    "card_number": "4242424242424242",
    "expiry_month": 12,
    "expiry_year": SystemClock().today().year + 2,  # validated against the real clock, so never hard-coded
    "cvv": "123",
    "holder_name": "Rahul Sharma",
}
CONTACT = {"guest_name": "Rahul Sharma", "guest_email": "rahul@example.com", "guest_phone": "9876543210"}

# (method, path template) -> whether the Idempotency-Key header is required
IDEMPOTENT_OPERATIONS = {
    ("post", f"{API}/bookings"): False,
    ("post", f"{API}/bookings/{{booking_id}}/payments"): True,
    ("post", f"{API}/bookings/{{booking_id}}/cancel"): False,
    ("post", f"{API}/catalog/owner-accounts"): False,
    ("post", f"{API}/catalog/owner-accounts/{{account_id}}/properties"): False,
    ("post", f"{API}/catalog/properties/{{property_id}}/room-types"): False,
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    GENERATOR_STATS.reset()
    document = SchemaGenerator().get_schema(request=None, public=True)
    warnings, errors = dict(GENERATOR_STATS._warn_cache), dict(GENERATOR_STATS._error_cache)
    assert (warnings, errors) == ({}, {}), "drf-spectacular reported problems while generating the schema"
    return document


def test_schema_validates_without_warnings(schema: dict[str, Any]) -> None:
    validate_schema(schema)  # raises on an invalid OpenAPI 3 document

    assert schema["openapi"].startswith("3.")
    assert f"{API}/bookings/{{booking_id}}/payments" in schema["paths"]


@pytest.mark.django_db
def test_schema_endpoint_serves_the_document(api_client: APIClient) -> None:
    response = api_client.get(f"{API}/schema", {"format": "json"})

    assert response.status_code == 200
    assert f"{API}/bookings" in response.json()["paths"]


@pytest.mark.parametrize(("operation", "required"), IDEMPOTENT_OPERATIONS.items(), ids=lambda value: str(value))
def test_write_operations_declare_the_idempotency_key(
    schema: dict[str, Any], operation: tuple[str, str], required: bool
) -> None:
    method, path = operation
    headers = {
        parameter["name"]: parameter.get("required", False)
        for parameter in schema["paths"][path][method].get("parameters", [])
        if parameter.get("in") == "header"
    }

    assert headers.get("Idempotency-Key") is required


# --- Responses against the document --------------------------------------------------------------------------


def _nullable_as_json_schema(node: Any) -> Any:
    """OpenAPI 3.0 schema -> JSON Schema 2020-12: ``nullable: true`` becomes "or null"."""
    if isinstance(node, list):
        return [_nullable_as_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {key: _nullable_as_json_schema(value) for key, value in node.items() if key != "nullable"}
    if not node.get("nullable"):
        return converted
    if "type" in converted and "$ref" not in converted:
        converted["type"] = [converted["type"], "null"]
        if "enum" in converted:
            converted["enum"] = [*converted["enum"], None]
        return converted
    return {"anyOf": [converted, {"type": "null"}]}


class ContractClient:
    """An APIClient whose every response is validated against the documented response schema."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._client = APIClient()
        self._paths = schema["paths"]
        self._components = _nullable_as_json_schema(deepcopy(schema["components"]))
        self.checked: list[tuple[str, str, int]] = []

    def get(self, path: str, data: dict[str, Any] | None = None, **extra: Any) -> Any:
        return self._checked("get", path, self._client.get(path, data, **extra))

    def post(self, path: str, data: dict[str, Any] | None = None, **extra: Any) -> Any:
        return self._checked("post", path, self._client.post(path, data, **extra))

    def _checked(self, method: str, path: str, response: Any) -> Any:
        template = self._template(path)
        documented = self._paths[template][method]["responses"]
        assert str(response.status_code) in documented, f"{method.upper()} {template}: undocumented {response.status_code}"
        media = documented[str(response.status_code)]["content"]["application/json"]["schema"]
        validator = Draft202012Validator({**_nullable_as_json_schema(media), "components": self._components})
        problems = [f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in validator.iter_errors(response.json())]
        assert problems == [], f"{method.upper()} {template} {response.status_code} breaks its schema: {problems}"
        self.checked.append((method, template, response.status_code))
        return response

    def _template(self, path: str) -> str:
        bare = path.split("?")[0]
        for template in self._paths:
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), bare):
                return template
        raise AssertionError(f"{path} is not in the schema")


def _data(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.json()
    return response.json()["data"]


@pytest.fixture
def client(schema: dict[str, Any]) -> Iterator[ContractClient]:
    yield ContractClient(schema)


@pytest.mark.django_db
def test_booking_journey_responses_match_the_schema(client: ContractClient) -> None:
    owner, guest = auth_header(OWNER, "OWNER"), auth_header(GUEST, "GUEST")
    client.get(f"{API}/health")
    client.get(f"{API}/catalog/amenities")

    # Onboarding, then the public views of the property.
    account = _data(
        client.post(
            f"{API}/catalog/owner-accounts",
            {"display_name": "Contract Stays", "contact_email": "owner@contract.example", "contact_phone": "9123456780"},
            **owner,
        ),
        201,
    )
    property_ = _data(
        client.post(
            f"{API}/catalog/owner-accounts/{account['account_id']}/properties",
            {
                "name": "Contract House",
                "property_type": "HOTEL",
                "star_rating": 4,
                "address": {
                    "line1": "7 Marine Drive",
                    "locality": "Churchgate",
                    "city": "Mumbai",
                    "state": "Maharashtra",
                    "postal_code": "400020",
                },
                "amenities": ["WIFI", "POOL"],
                "pricing_strategy": "WEEKEND",
                "cancellation_policy": "MODERATE",
                "room_types": [{"name": "Deluxe", "max_occupancy": 2, "total_rooms": 3, "base_price": "5000.00"}],
            },
            **owner,
        ),
        201,
    )
    room_type_id = property_["room_types"][0]["room_type_id"]
    client.get(f"{API}/catalog/properties/{property_['property_id']}")
    client.get(f"{API}/catalog/owner-accounts/{account['account_id']}/properties", **owner)
    check_in = SystemClock().today() + timedelta(days=21)
    stay = {"check_in": check_in.isoformat(), "check_out": (check_in + timedelta(days=2)).isoformat()}
    assert _data(client.get(f"{API}/discovery/search", {"city": "mumbai", "guests": 2, **stay}))["items"]
    client.get(f"{API}/discovery/search", {"city": "mumbai", "guests": 0, **stay})  # 400

    # Book, fail to pay, pay, read, list, cancel.
    book = {"room_type_id": room_type_id, "guests": 2, **stay, **CONTACT}
    booking = _data(client.post(f"{API}/bookings", book, HTTP_IDEMPOTENCY_KEY="contract-book-1", **guest), 201)
    pay_url = f"{API}/bookings/{booking['booking_id']}/payments"
    declined = {"method": "CARD", "card": {**CARD, "card_number": "4000000000000002"}}
    assert client.post(pay_url, declined, HTTP_IDEMPOTENCY_KEY="contract-pay-1", **guest).status_code == 402
    paid = _data(client.post(pay_url, {"method": "CARD", "card": CARD}, HTTP_IDEMPOTENCY_KEY="contract-pay-2", **guest))
    client.post(pay_url, {"method": "CARD", "card": CARD}, HTTP_IDEMPOTENCY_KEY="contract-pay-3", **guest)  # 409
    client.post(pay_url, {"method": "CARD", "card": CARD}, **guest)  # 400: the key is required
    client.get(f"{API}/payments/{paid['payment']['payment_id']}", **guest)
    client.get(f"{API}/bookings/{booking['booking_id']}", **guest)
    client.get(f"{API}/bookings/{booking['booking_id']}", **owner)  # masked contact
    client.get(f"{API}/bookings", **guest)
    client.get(f"{API}/bookings", {"property_id": property_["property_id"]}, **owner)
    client.get(f"{API}/bookings", **owner)  # 400: owners must filter by property
    cancel_url = f"{API}/bookings/{booking['booking_id']}/cancel"
    assert _data(client.post(cancel_url, {"reason": "contract"}, **guest))["status"] == "CANCELLED"
    client.post(cancel_url, {"reason": "again"}, **guest)  # 409

    # A lapsed hold: the pay answers 409 with the expired booking in data.
    lapsed = _data(client.post(f"{API}/bookings", book, HTTP_IDEMPOTENCY_KEY="contract-book-2", **guest), 201)
    BookingRow.objects.filter(pk=lapsed["booking_id"]).update(hold_expires_at=timezone.now() - timedelta(minutes=1))
    expired = client.post(
        f"{API}/bookings/{lapsed['booking_id']}/payments",
        {"method": "CARD", "card": CARD},
        HTTP_IDEMPOTENCY_KEY="contract-pay-4",
        **guest,
    )
    assert (expired.status_code, expired.json()["data"]["booking"]["status"]) == (409, "EXPIRED")

    # Errors with no data.
    client.post(f"{API}/bookings", {**book, "guests": 3}, **guest)  # 422 over capacity
    client.post(f"{API}/bookings", book)  # 401
    client.get(f"{API}/bookings/{uuid.uuid4()}", **guest)  # 404

    statuses = {status for _, _, status in client.checked}
    assert {200, 201, 400, 401, 402, 404, 409, 422} <= statuses, client.checked


def test_contract_check_rejects_what_the_schema_does_not_allow() -> None:
    """Negative control for the conversion and validator used above, on a two-field envelope."""
    components = {
        "schemas": {
            "Item": {
                "type": "object",
                "required": ["id", "note"],
                "properties": {"id": {"type": "string"}, "note": {"type": "string", "nullable": True}},
            }
        }
    }
    validator = Draft202012Validator(
        {"$ref": "#/components/schemas/Item", "components": _nullable_as_json_schema(components)}
    )

    assert validator.is_valid({"id": "a", "note": None})  # nullable: the type or null
    assert not validator.is_valid({"id": None, "note": "x"})  # null in a non-nullable field
    assert not validator.is_valid({"id": "a"})  # a required field missing
    assert not validator.is_valid([{"id": "a", "note": "x"}])  # an array where an object is documented
