"""API tests for ``GET /hotel/v1/discovery/search`` (LLD §10.2 row 10, §10.4). Need the database.

Listings arrive the production way: a catalog event published on the in-process bus reaches the projector
that ``DiscoveryConfig.ready()`` subscribed. Inventory and pricing are the real contexts.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from catalog.public.dto import PropertySnapshot, RoomTypeSummary
from catalog.public.enums import PricingStrategyCode, PropertyType
from catalog.public.events import PropertyOnboarded, RoomTypeAdded
from catalog.tests.openapi_contract import assert_matches_schema
from discovery.models.listing_models import Listing as ListingRow
from hotel_commons.domain.clock import business_date
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.events.in_process import event_bus
from inventory.repositories.room_night_repository import DjangoRoomNightRepository
from inventory.public.api import get_inventory_api

pytestmark = pytest.mark.django_db

PROPERTY_ID = UUID("0192f5c4-0000-7000-8000-000000000001")
DELUXE_ID = UUID("0192f5c4-0000-7000-8000-000000000002")
SUITE_ID = UUID("0192f5c4-0000-7000-8000-000000000003")
TODAY = business_date(datetime.now(UTC))
HORIZON_END = TODAY + timedelta(days=settings.INVENTORY_HORIZON_DAYS)
CHECK_IN = TODAY + timedelta(days=10)
CHECK_OUT = CHECK_IN + timedelta(days=2)


def _room_type(room_type_id: UUID, name: str, rupees: int, amenities: set[str]) -> RoomTypeSummary:
    return RoomTypeSummary(
        room_type_id=room_type_id,
        name=name,
        max_occupancy=2,
        total_rooms=3,
        base_price=Money.of_rupees(rupees),
        amenity_codes=frozenset(amenities),
        is_active=True,
    )


DEFAULT_ROOM_TYPES = (
    _room_type(DELUXE_ID, "Deluxe Sea View", 4000, {"POOL"}),
    _room_type(SUITE_ID, "Suite", 6000, {"BALCONY"}),
)


def _snapshot(name: str = "Sea Breeze Resort") -> PropertySnapshot:
    return PropertySnapshot(
        property_id=PROPERTY_ID,
        owner_account_id=UUID(int=1),
        name=name,
        property_type=PropertyType.RESORT,
        star_rating=4,
        city="Goa",
        locality="Candolim",
        amenity_codes=frozenset({"WIFI"}),
        pricing_strategy=PricingStrategyCode.STANDARD,
        is_active=True,
        room_types=DEFAULT_ROOM_TYPES,
    )


@pytest.fixture
def onboarded_resort() -> PropertySnapshot:
    snapshot = _snapshot()
    event_bus.publish(PropertyOnboarded(property=snapshot))
    inventory = get_inventory_api()
    for room_type in snapshot.room_types:
        inventory.provision(room_type.room_type_id, room_type.total_rooms)
    return snapshot


def _search(client: APIClient, **params: Any) -> Any:
    query = {"city": "goa", "check_in": CHECK_IN.isoformat(), "check_out": CHECK_OUT.isoformat(), "guests": 2}
    query.update(params)
    return client.get(reverse("discovery-search"), query)


def test_search_is_public_and_returns_priced_offers(api_client: APIClient, onboarded_resort: PropertySnapshot) -> None:
    response = _search(api_client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 2100000
    data = body["data"]
    assert (data["page"], data["page_size"], data["total"]) == (1, 20, 1)
    [item] = data["items"]
    assert item["property_id"] == str(PROPERTY_ID)
    assert item["name"] == "Sea Breeze Resort"
    assert item["property_type"] == "RESORT"
    assert item["star_rating"] == 4
    assert (item["city"], item["locality"]) == ("Goa", "Candolim")
    assert item["amenities"] == ["WIFI"]
    assert (item["lowest_nightly_price"], item["currency"]) == ("4000.00", "INR")
    deluxe, suite = item["offers"]
    assert deluxe == {
        "room_type_id": str(DELUXE_ID),
        "room_type": "Deluxe Sea View",
        "max_occupancy": 2,
        "available_rooms": 3,
        "pricing_strategy": "STANDARD",
        "nightly_rates": [
            {"date": CHECK_IN.isoformat(), "price": "4000.00"},
            {"date": (CHECK_IN + timedelta(days=1)).isoformat(), "price": "4000.00"},
        ],
        "total": "8000.00",
        "average_nightly": "4000.00",
        "currency": "INR",
    }
    assert suite["average_nightly"] == "6000.00"


def test_search_applies_amenity_and_price_filters(api_client: APIClient, onboarded_resort: PropertySnapshot) -> None:
    by_amenity = _search(api_client, amenities="wifi,POOL").json()["data"]
    by_price = _search(api_client, min_price="5000", max_price="6000.00").json()["data"]
    too_cheap = _search(api_client, max_price="3999.99").json()["data"]

    assert [offer["room_type"] for offer in by_amenity["items"][0]["offers"]] == ["Deluxe Sea View"]
    assert [offer["room_type"] for offer in by_price["items"][0]["offers"]] == ["Suite"]
    assert (too_cheap["items"], too_cheap["total"]) == ([], 0)


def test_sold_out_room_type_is_not_offered(api_client: APIClient, onboarded_resort: PropertySnapshot) -> None:
    get_inventory_api().hold(
        room_type_id=SUITE_ID, stay=DateRange(CHECK_IN, CHECK_OUT), rooms=3, reference="test-sold-out", expires_at=None
    )

    data = _search(api_client).json()["data"]

    assert [offer["room_type"] for offer in data["items"][0]["offers"]] == ["Deluxe Sea View"]


def test_other_city_and_property_type_find_nothing(api_client: APIClient, onboarded_resort: PropertySnapshot) -> None:
    assert _search(api_client, city="Mumbai").json()["data"]["total"] == 0
    assert _search(api_client, property_types="HOTEL,VILLA").json()["data"]["total"] == 0
    assert _search(api_client, property_types="RESORT").json()["data"]["total"] == 1


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"check_out": CHECK_IN.isoformat()}, "check_out"),
        ({"check_out": (CHECK_IN + timedelta(days=31)).isoformat()}, "check_out"),
        ({"guests": 0}, "guests"),
        ({"sort": "cheapest"}, "sort"),
        ({"property_types": "CASTLE"}, "property_types"),
        ({"min_price": "0.105"}, "min_price"),
        ({"min_price": "5000", "max_price": "4000"}, "max_price"),
        ({"page_size": 51}, "page_size"),
        ({"amenities": "WIFI,POLL"}, "amenities"),
        ({"amenities": "foo"}, "amenities"),
    ],
)
def test_invalid_query_returns_validation_envelope(
    api_client: APIClient, params: dict[str, Any], field: str
) -> None:
    response = _search(api_client, **params)

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 2100001
    assert any(key == field or key.startswith(f"{field}.") for key in body["errors"])  # "x.0": a list item
    assert body["data"] is None


@pytest.mark.parametrize(
    ("check_in", "check_out", "field"),
    [
        (TODAY - timedelta(days=2), TODAY, "check_in"),
        (TODAY - timedelta(days=1), TODAY + timedelta(days=1), "check_in"),
        (HORIZON_END - timedelta(days=1), HORIZON_END + timedelta(days=1), "check_out"),
    ],
)
def test_stay_outside_the_booking_window_is_refused(
    api_client: APIClient, onboarded_resort: PropertySnapshot, check_in: date, check_out: date, field: str
) -> None:
    for room_type in onboarded_resort.room_types:  # the nights have rows, as they do once time passes
        DjangoRoomNightRepository().ensure_rows(room_type.room_type_id, 3, DateRange(check_in, check_out).dates())

    response = _search(api_client, check_in=check_in.isoformat(), check_out=check_out.isoformat())

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 2100001
    assert set(body["errors"]) == {field}
    assert body["data"] is None


def test_stays_from_today_to_the_horizon_are_searchable(
    api_client: APIClient, onboarded_resort: PropertySnapshot
) -> None:
    first = _search(api_client, check_in=TODAY.isoformat(), check_out=(TODAY + timedelta(days=1)).isoformat())
    last = _search(
        api_client,
        check_in=(HORIZON_END - timedelta(days=1)).isoformat(),
        check_out=HORIZON_END.isoformat(),
    )

    assert (first.status_code, first.json()["data"]["total"]) == (200, 1)
    assert last.status_code == 200


def test_missing_required_parameters_are_reported(api_client: APIClient) -> None:
    response = api_client.get(reverse("discovery-search"))

    assert response.status_code == 400
    assert {"city", "check_in", "check_out", "guests"} <= set(response.json()["errors"])


def test_sort_by_rating_is_accepted(api_client: APIClient, onboarded_resort: PropertySnapshot) -> None:
    response = _search(api_client, sort="rating_desc")

    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1


def test_room_type_added_reprojects_without_duplicates(onboarded_resort: PropertySnapshot) -> None:
    renamed = _snapshot("Sea Breeze Beach Resort")

    event_bus.publish(RoomTypeAdded(property=renamed, room_type_id=SUITE_ID))

    rows = ListingRow.objects.filter(property_id=PROPERTY_ID).order_by("room_type_name")
    assert [(row.room_type_name, row.property_name) for row in rows] == [
        ("Deluxe Sea View", "Sea Breeze Beach Resort"),
        ("Suite", "Sea Breeze Beach Resort"),
    ]
    assert all(row.updation_date is not None for row in rows)
    assert sorted(rows[0].amenity_codes) == ["POOL", "WIFI"]


def test_search_responses_match_the_published_schema(api_client: APIClient) -> None:
    unrated = replace(_snapshot("Unrated Homestay"), star_rating=None, property_type=PropertyType.HOMESTAY)
    event_bus.publish(PropertyOnboarded(property=unrated))
    for room_type in unrated.room_types:
        get_inventory_api().provision(room_type.room_type_id, room_type.total_rooms)

    found = _search(api_client)
    refused = _search(api_client, amenities="HELIPAD")

    assert found.json()["data"]["items"][0]["star_rating"] is None
    assert (found.status_code, refused.status_code) == (200, 400)
    assert_matches_schema(found, "/hotel/v1/discovery/search", "get")
    assert_matches_schema(refused, "/hotel/v1/discovery/search", "get")


def _dynamic_property(index: int, room_types: int) -> PropertySnapshot:
    snapshot = replace(
        _snapshot(f"Dynamic {index}"),
        property_id=UUID(int=1000 + index),
        pricing_strategy=PricingStrategyCode.DYNAMIC,
        room_types=tuple(
            _room_type(UUID(int=10_000 + index * 100 + n), f"Room {n}", 4000, set()) for n in range(room_types)
        ),
    )
    event_bus.publish(PropertyOnboarded(property=snapshot))
    for room_type in snapshot.room_types:
        get_inventory_api().provision(room_type.room_type_id, room_type.total_rooms)
    return snapshot


def test_search_query_count_does_not_grow_with_dynamic_listings(api_client: APIClient) -> None:
    """B3: occupancy for every DYNAMIC candidate is read in one query, not one per room type."""
    _dynamic_property(1, 2)
    with CaptureQueriesContext(connection) as few:
        assert _search(api_client).json()["data"]["total"] == 1
    _dynamic_property(2, 3)
    _dynamic_property(3, 3)
    with CaptureQueriesContext(connection) as many:
        assert _search(api_client).json()["data"]["total"] == 3

    assert len(many) == len(few) == 3  # listings, availability, occupancy
