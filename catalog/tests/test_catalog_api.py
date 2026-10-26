"""Catalog HTTP API end to end against Postgres (LLD §10.2 rows 4-9). Every test here needs the database."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from hotel_commons.exceptions import Conflict
from hotel_commons.domain.clock import SystemClock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.security.principal import Principal, Role
from hotel_commons.status_code import NOT_FOUND, VALIDATION_FAILED
from hotel_commons.testing.unit_of_work import NoopUnitOfWork
from inventory.public.api import get_inventory_api

from catalog.domain.property import Property as DomainProperty
from catalog.domain.room_type import RoomType
from catalog.models.catalog_models import Property
from catalog.repositories.property_repository import DjangoPropertyRepository
from catalog.serializers.owner_account_serializers import OwnerAccountSerializer
from catalog.services.owner_account_service import OwnerAccountService
from catalog.status_code import (
    DUPLICATE_ROOM_TYPE_NAME,
    OWNER_ACCOUNT_EXISTS,
    PROPERTY_NOT_FOUND,
    PROPERTY_TYPE_RULE_VIOLATED,
    UNKNOWN_AMENITIES,
)
from catalog.tests.fakes import (
    KNOWN_AMENITIES,
    InMemoryOwnerAccountRepository,
    owner_draft,
    property_draft,
    room_type_draft,
)
from catalog.tests.openapi_contract import assert_matches_schema

pytestmark = pytest.mark.django_db

API = "/hotel/v1/catalog"


def account_payload(email: str = "owner@example.com") -> dict:
    return {"display_name": "Coastal Stays", "contact_email": email, "contact_phone": "9876543210"}


def room_type_payload(name: str = "Deluxe Sea View", **overrides: object) -> dict:
    return {
        "name": name,
        "max_occupancy": 2,
        "total_rooms": 5,
        "base_price": "4800.50",
        "amenities": ["TV", "AIR_CONDITIONING"],
        **overrides,
    }


def property_payload(**overrides: object) -> dict:
    return {
        "name": "Sea Breeze Resort",
        "property_type": "RESORT",
        "star_rating": 4,
        "address": {
            "line1": "Beach Road",
            "locality": "Candolim",
            "city": "Goa",
            "state": "Goa",
            "postal_code": "403515",
        },
        "amenities": ["WIFI", "POOL"],
        "pricing_strategy": "WEEKEND",
        "cancellation_policy": "MODERATE",
        "room_types": [room_type_payload()],
        **overrides,
    }


@pytest.fixture
def open_account(api_client, auth):
    def _open(user_id: str = "owner-1", email: str = "owner@example.com") -> dict:
        response = api_client.post(f"{API}/owner-accounts", account_payload(email), **auth(user_id, "OWNER"))
        assert response.status_code == 201, response.json()
        return response.json()["data"]

    return _open


@pytest.fixture
def onboard(api_client, auth):
    def _onboard(account_id: str, user_id: str = "owner-1", **overrides: object):
        url = f"{API}/owner-accounts/{account_id}/properties"
        return api_client.post(url, property_payload(**overrides), **auth(user_id, "OWNER"))

    return _onboard


def test_amenities_are_public(api_client):
    response = api_client.get(f"{API}/amenities")

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["data"]}
    assert {"WIFI", "POOL", "KITCHEN"} <= codes
    assert {item["category"] for item in response.json()["data"]} == {"PROPERTY", "ROOM"}


def test_owner_opens_account_and_sees_own_contact(open_account):
    account = open_account()

    assert account["owner_user_id"] == "owner-1"
    assert account["contact_email"] == "owner@example.com"
    assert account["contact_phone"] == "9876543210"
    assert account["created_at"].endswith("Z")


def test_contact_is_encrypted_at_rest(open_account):
    account = open_account()

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT contact_email, contact_phone, masked_contact_email FROM "catalog"."owner_account" WHERE id = %s',
            [account["account_id"]],
        )
        email, phone, masked_email = cursor.fetchone()
        cursor.execute(
            'SELECT contact_email FROM "catalog"."owner_account_log" WHERE log_id = %s', [account["account_id"]]
        )
        [logged_email] = cursor.fetchone()

    assert email.startswith("ENC_") and phone.startswith("ENC_") and logged_email.startswith("ENC_")
    assert masked_email == "o****@example.com"


def test_contact_is_masked_for_another_owner():
    service = OwnerAccountService(InMemoryOwnerAccountRepository(), NoopUnitOfWork())
    opened = service.open_account("owner-1", owner_draft())
    request = SimpleNamespace(principal=Principal(user_id="owner-2", roles=frozenset({Role.OWNER})))

    data = OwnerAccountSerializer(opened, context={"request": request}).data

    assert data["contact_email"] == "o****@example.com"
    assert data["contact_phone"] == "******3210"


@pytest.mark.parametrize(("headers_for", "expected_status"), [(None, 401), (("guest-1", "GUEST"), 403)])
def test_open_account_requires_owner_or_admin(api_client, auth, headers_for, expected_status):
    headers = auth(*headers_for) if headers_for else {}

    response = api_client.post(f"{API}/owner-accounts", account_payload(), **headers)

    assert response.status_code == expected_status


def test_second_account_with_same_email_conflicts(api_client, auth, open_account):
    open_account("owner-1", "owner@example.com")

    payload = account_payload("OWNER@example.com")

    response = api_client.post(f"{API}/owner-accounts", payload, **auth("owner-2", "OWNER"))

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == OWNER_ACCOUNT_EXISTS.code
    assert body["message"] == "Owner account could not be created with these contact details"
    assert "email" not in body["message"].lower() and "exists" not in body["message"].lower()
    assert body["data"] is None


def test_invalid_contact_phone_is_400(api_client, auth):
    payload = {**account_payload(), "contact_phone": "12345"}

    response = api_client.post(f"{API}/owner-accounts", payload, **auth("owner-1", "OWNER"))

    assert response.status_code == 400
    assert response.json()["status"] == VALIDATION_FAILED.code
    assert "contact_phone" in response.json()["errors"]


def test_onboard_property_returns_201_with_paisa_rendered_as_rupees(open_account, onboard):
    account = open_account()

    response = onboard(account["account_id"])

    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["owner_account_id"] == account["account_id"]
    assert data["property_type"] == "RESORT"
    assert data["amenities"] == ["POOL", "WIFI"]
    assert data["check_in_time"] == "14:00" and data["check_out_time"] == "11:00"
    [room_type] = data["room_types"]
    assert room_type["base_price"] == "4800.50" and room_type["currency"] == "INR"
    assert room_type["amenities"] == ["AIR_CONDITIONING", "TV"]


def test_onboarding_provisions_inventory(open_account, onboard):
    account = open_account()
    room_type_id = onboard(account["account_id"]).json()["data"]["room_types"][0]["room_type_id"]
    tomorrow = SystemClock().today() + timedelta(days=1)

    available = get_inventory_api().availability([room_type_id], DateRange(tomorrow, tomorrow + timedelta(days=2)), 1)

    assert list(available.values()) == [5]


def test_another_owner_cannot_onboard_or_list_on_my_account(api_client, auth, open_account, onboard):
    account = open_account("owner-1")

    onboard_response = onboard(account["account_id"], user_id="owner-2")
    list_url = f"{API}/owner-accounts/{account['account_id']}/properties"
    list_response = api_client.get(list_url, **auth("owner-2", "OWNER"))

    assert onboard_response.status_code == 404
    assert list_response.status_code == 404
    assert not Property.objects.exists()


def test_owner_lists_own_properties_and_admin_lists_any(api_client, auth, open_account, onboard):
    account = open_account("owner-1")
    onboard(account["account_id"], name="Sea Breeze Resort")
    onboard(account["account_id"], name="Sea Breeze Annexe")
    url = f"{API}/owner-accounts/{account['account_id']}/properties"

    own = api_client.get(url, **auth("owner-1", "OWNER"))
    admin = api_client.get(url, **auth("admin-1", "ADMIN"))

    assert [p["name"] for p in own.json()["data"]] == ["Sea Breeze Resort", "Sea Breeze Annexe"]
    assert admin.json()["data"] == own.json()["data"]


def test_hotel_without_star_rating_is_422(open_account, onboard):
    account = open_account()

    response = onboard(account["account_id"], property_type="HOTEL", star_rating=None)

    assert response.status_code == 422
    assert response.json()["status"] == PROPERTY_TYPE_RULE_VIOLATED.code


def test_unknown_amenity_is_400(open_account, onboard):
    account = open_account()

    response = onboard(account["account_id"], amenities=["WIFI", "HELIPAD"])

    assert response.status_code == 400
    assert response.json()["status"] == UNKNOWN_AMENITIES.code
    assert response.json()["message"] == "Unknown amenity codes: HELIPAD"


@pytest.mark.parametrize("base_price", ["0.50", "0.105", "-1", "abc"])
def test_invalid_base_price_is_400(open_account, onboard, base_price):
    account = open_account()

    response = onboard(account["account_id"], room_types=[room_type_payload(base_price=base_price)])

    assert response.status_code == 400
    assert any(key.split(".")[0] == "room_types" for key in response.json()["errors"])  # e.g. room_types.0.base_price


def test_property_details_are_public(api_client, open_account, onboard):
    property_id = onboard(open_account()["account_id"]).json()["data"]["property_id"]

    response = api_client.get(f"{API}/properties/{property_id}")

    assert response.status_code == 200
    assert response.json()["data"]["property_id"] == property_id


def test_unknown_or_malformed_property_id_is_404(api_client):
    unknown = api_client.get(f"{API}/properties/{uuid4()}")
    malformed = api_client.get(f"{API}/properties/not-a-uuid")

    assert unknown.status_code == 404 and unknown.json()["status"] == PROPERTY_NOT_FOUND.code
    assert malformed.status_code == 404 and malformed.json()["status"] == NOT_FOUND.code


def test_add_room_type_and_duplicate_name_conflicts(api_client, auth, open_account, onboard):
    property_id = onboard(open_account()["account_id"]).json()["data"]["property_id"]
    url = f"{API}/properties/{property_id}/room-types"

    added = api_client.post(url, room_type_payload("Family Suite", max_occupancy=4), **auth("owner-1", "OWNER"))
    duplicate = api_client.post(url, room_type_payload("FAMILY suite"), **auth("owner-1", "OWNER"))

    assert added.status_code == 201
    assert added.json()["data"]["name"] == "Family Suite"
    assert duplicate.status_code == 409
    assert duplicate.json()["status"] == DUPLICATE_ROOM_TYPE_NAME.code


def test_onboarding_api_maps_greek_final_sigma_duplicate_to_409(api_client, auth, open_account, onboard):
    response = onboard(
        open_account()["account_id"], room_types=[room_type_payload("ΟΔΟΣ Suite"), room_type_payload("οδοσ suite")]
    )

    assert response.status_code == 409
    assert response.json()["status"] == DUPLICATE_ROOM_TYPE_NAME.code


def test_repository_maps_a_duplicate_name_the_domain_missed_to_409(open_account):
    """The unique constraint is the source of truth: onboarding (``add``) maps its violation like ``add_room_type``."""
    account_id = open_account()["account_id"]
    property_ = DomainProperty.onboard(
        owner_account_id=UUID(account_id),
        draft=property_draft(room_types=(room_type_draft("Deluxe"),)),
        known_amenities=KNOWN_AMENITIES,
        rules=(),
    )
    property_.room_types.append(RoomType.create(property_id=property_.id, draft=room_type_draft("DELUXE")))

    with pytest.raises(Conflict) as error, transaction.atomic():
        DjangoPropertyRepository().add(property_)

    assert error.value.status_code == DUPLICATE_ROOM_TYPE_NAME
    assert not Property.objects.filter(pk=property_.id).exists()


def test_villa_rejects_a_second_room_type(api_client, auth, open_account, onboard):
    villa = onboard(
        open_account()["account_id"],
        name="Casa Anjuna",
        property_type="VILLA",
        star_rating=None,
        room_types=[room_type_payload("Entire Villa", max_occupancy=8, total_rooms=1)],
    )
    url = f"{API}/properties/{villa.json()['data']['property_id']}/room-types"

    response = api_client.post(url, room_type_payload("Cottage", total_rooms=1), **auth("owner-1", "OWNER"))

    assert villa.status_code == 201
    assert response.status_code == 422


def test_another_owner_cannot_add_room_types(api_client, auth, open_account, onboard):
    property_id = onboard(open_account("owner-1")["account_id"]).json()["data"]["property_id"]

    response = api_client.post(
        f"{API}/properties/{property_id}/room-types", room_type_payload("Suite"), **auth("owner-2", "OWNER")
    )

    assert response.status_code == 404


def test_seed_demo_is_idempotent():
    call_command("seed_demo")
    call_command("seed_demo")

    assert Property.objects.count() == 3
    assert set(Property.objects.values_list("city_key", flat=True)) == {"goa", "bengaluru"}


def test_catalog_responses_match_the_published_schema(api_client, auth, open_account, onboard):
    """A2/A3: list endpoints document one envelope object, and nullable fields are documented as nullable."""
    owner = auth("owner-1", "OWNER")
    amenities = api_client.get(f"{API}/amenities")
    created = api_client.post(f"{API}/owner-accounts", account_payload("contract@example.com"), **owner)
    account_id = created.json()["data"]["account_id"]
    onboarded = onboard(account_id, star_rating=None, property_type="HOMESTAY")
    property_id = onboarded.json()["data"]["property_id"]
    listed = api_client.get(f"{API}/owner-accounts/{account_id}/properties", **owner)
    retrieved = api_client.get(f"{API}/properties/{property_id}")
    room_type = api_client.post(f"{API}/properties/{property_id}/room-types", room_type_payload("Annex"), **owner)
    duplicate = api_client.post(f"{API}/owner-accounts", account_payload("contract@example.com"), **owner)

    assert [r.status_code for r in (amenities, created, onboarded, listed, retrieved, room_type, duplicate)] == [
        200, 201, 201, 200, 200, 201, 409
    ]
    assert retrieved.json()["data"]["star_rating"] is None
    assert_matches_schema(amenities, "/hotel/v1/catalog/amenities", "get")
    assert_matches_schema(created, "/hotel/v1/catalog/owner-accounts", "post")
    assert_matches_schema(duplicate, "/hotel/v1/catalog/owner-accounts", "post")
    assert_matches_schema(onboarded, "/hotel/v1/catalog/owner-accounts/{account_id}/properties", "post")
    assert_matches_schema(listed, "/hotel/v1/catalog/owner-accounts/{account_id}/properties", "get")
    assert_matches_schema(retrieved, "/hotel/v1/catalog/properties/{property_id}", "get")
    assert_matches_schema(room_type, "/hotel/v1/catalog/properties/{property_id}/room-types", "post")
