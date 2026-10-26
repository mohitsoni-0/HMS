"""Unit tests for the listing projection of catalog events (LLD §7.2). Pure Python, no Django."""

from uuid import UUID

from catalog.public.dto import PropertySnapshot, RoomTypeSummary
from catalog.public.enums import PricingStrategyCode, PropertyType
from catalog.public.events import PropertyOnboarded, RoomTypeAdded
from discovery.domain.listing import Listing
from discovery.services.listing_projector import ListingProjector
from discovery.tests.fakes import InMemoryListingRepository
from hotel_commons.domain.money import Money

PROPERTY_ID = UUID(int=100)
DELUXE_ID = UUID(int=101)
SUITE_ID = UUID(int=102)
VILLA_ROOM_ID = UUID(int=103)


def _room_type(room_type_id: UUID, name: str, *, is_active: bool = True, rupees: int = 4000) -> RoomTypeSummary:
    return RoomTypeSummary(
        room_type_id=room_type_id,
        name=name,
        max_occupancy=2,
        total_rooms=10,
        base_price=Money.of_rupees(rupees),
        amenity_codes=frozenset({"WIFI", "TV"}),
        is_active=is_active,
    )


def _snapshot(*room_types: RoomTypeSummary, is_active: bool = True) -> PropertySnapshot:
    return PropertySnapshot(
        property_id=PROPERTY_ID,
        owner_account_id=UUID(int=1),
        name="Sea Breeze Resort",
        property_type=PropertyType.RESORT,
        star_rating=4,
        city=" Goa ",
        locality="Candolim",
        amenity_codes=frozenset({"POOL", "WIFI"}),
        pricing_strategy=PricingStrategyCode.WEEKEND,
        is_active=is_active,
        room_types=room_types,
    )


def test_projector_upserts_one_listing_per_room_type() -> None:
    repository = InMemoryListingRepository()
    projector = ListingProjector(repository)
    snapshot = _snapshot(_room_type(DELUXE_ID, "Deluxe"), _room_type(SUITE_ID, "Suite", is_active=False, rupees=7000))

    projector.on_property_onboarded(PropertyOnboarded(property=snapshot))

    assert set(repository.rows) == {DELUXE_ID, SUITE_ID}
    assert repository.rows[DELUXE_ID] == Listing(
        room_type_id=DELUXE_ID,
        property_id=PROPERTY_ID,
        property_name="Sea Breeze Resort",
        property_type=PropertyType.RESORT,
        star_rating=4,
        city="Goa",
        city_key="goa",
        locality="Candolim",
        amenity_codes=frozenset({"POOL", "WIFI", "TV"}),
        room_type_name="Deluxe",
        max_occupancy=2,
        base_price=Money.of_rupees(4000),
        pricing_strategy=PricingStrategyCode.WEEKEND,
        is_active=True,
    )
    assert repository.rows[SUITE_ID].is_active is False
    assert repository.rows[SUITE_ID].base_price == Money.of_rupees(7000)


def test_inactive_property_projects_inactive_listings() -> None:
    repository = InMemoryListingRepository()

    ListingProjector(repository).on_property_onboarded(
        PropertyOnboarded(property=_snapshot(_room_type(DELUXE_ID, "Deluxe"), is_active=False))
    )

    assert repository.rows[DELUXE_ID].is_active is False


def test_room_type_added_reprojects_the_whole_property_once_per_room_type() -> None:
    repository = InMemoryListingRepository()
    projector = ListingProjector(repository)
    projector.on_property_onboarded(PropertyOnboarded(property=_snapshot(_room_type(DELUXE_ID, "Deluxe"))))

    grown = _snapshot(_room_type(DELUXE_ID, "Deluxe"), _room_type(VILLA_ROOM_ID, "Garden Room"))
    projector.on_room_type_added(RoomTypeAdded(property=grown, room_type_id=VILLA_ROOM_ID))

    assert set(repository.rows) == {DELUXE_ID, VILLA_ROOM_ID}
    assert repository.rows[VILLA_ROOM_ID].room_type_name == "Garden Room"
