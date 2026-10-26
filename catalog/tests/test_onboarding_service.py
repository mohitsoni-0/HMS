"""Catalog use cases over in-memory repositories (LLD §4.4)."""

from uuid import uuid4

import pytest
from hotel_commons.exceptions import BusinessRuleViolation, Conflict, NotFound
from hotel_commons.testing.events import RecordingEventPublisher
from hotel_commons.testing.unit_of_work import NoopUnitOfWork

from catalog.enums import PricingStrategyCode, PropertyType
from catalog.public.events import PropertyOnboarded, RoomTypeAdded
from catalog.services.onboarding_service import PropertyOnboardingService
from catalog.services.owner_account_service import OwnerAccountService
from catalog.services.query_service import CatalogQueryService
from catalog.services.room_type_service import RoomTypeService
from catalog.status_code import (
    OWNER_ACCOUNT_EXISTS,
    OWNER_ACCOUNT_NOT_FOUND,
    PROPERTY_NOT_FOUND,
    ROOM_TYPE_NOT_FOUND,
)
from catalog.tests.fakes import (
    InMemoryAmenityRepository,
    InMemoryOwnerAccountRepository,
    InMemoryPropertyRepository,
    RecordingProvisioning,
    owner_draft,
    property_draft,
    room_type_draft,
    villa_draft,
)


class Catalog:
    """The four services wired to one set of fakes."""

    def __init__(self) -> None:
        self.accounts = InMemoryOwnerAccountRepository()
        self.properties = InMemoryPropertyRepository()
        self.amenities = InMemoryAmenityRepository()
        self.provisioning = RecordingProvisioning()
        self.events = RecordingEventPublisher()
        self.uow = NoopUnitOfWork()
        self.owner_accounts = OwnerAccountService(self.accounts, self.uow)
        self.onboarding = PropertyOnboardingService(
            self.accounts, self.properties, self.amenities, self.provisioning, self.events, self.uow
        )
        self.room_types = RoomTypeService(self.properties, self.amenities, self.provisioning, self.events, self.uow)
        self.queries = CatalogQueryService(self.accounts, self.properties, self.amenities)


@pytest.fixture
def catalog() -> Catalog:
    return Catalog()


def test_open_account_persists_owner(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())

    stored = catalog.accounts.get(account.id)
    assert stored.owner_user_id == "owner-1"
    assert stored.contact.email == "owner@example.com"
    assert catalog.uow.commits == 1


def test_second_account_with_same_email_conflicts(catalog):
    catalog.owner_accounts.open_account("owner-1", owner_draft("owner@example.com"))

    with pytest.raises(Conflict) as error:
        catalog.owner_accounts.open_account("owner-2", owner_draft("Owner@Example.com"))

    assert error.value.status_code == OWNER_ACCOUNT_EXISTS


def test_standalone_and_chain_use_same_path(catalog):
    chain = catalog.owner_accounts.open_account("owner-chain", owner_draft("chain@example.com", "Chain Group"))
    solo = catalog.owner_accounts.open_account("owner-solo", owner_draft("solo@example.com", "Solo Villas"))

    goa = catalog.onboarding.onboard(chain.id, property_draft(name="Sea Breeze Resort"))
    bengaluru = catalog.onboarding.onboard(
        chain.id, property_draft(name="Garden City Hotel", property_type=PropertyType.HOTEL, star_rating=3)
    )
    villa = catalog.onboarding.onboard(solo.id, villa_draft())

    assert {p.id for p in catalog.queries.list_properties(chain.id)} == {goa.id, bengaluru.id}
    assert [p.id for p in catalog.queries.list_properties(solo.id)] == [villa.id]
    assert len(catalog.events.of_type(PropertyOnboarded)) == 3


def test_onboard_unknown_account_is_not_found(catalog):
    with pytest.raises(NotFound) as error:
        catalog.onboarding.onboard(uuid4(), property_draft())

    assert error.value.status_code == OWNER_ACCOUNT_NOT_FOUND
    assert catalog.provisioning.calls == []


def test_onboarding_provisions_inventory_and_publishes_event(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())
    room_types = (room_type_draft("Deluxe", total_rooms=7), room_type_draft("Suite", total_rooms=2))
    draft = property_draft(room_types=room_types)

    property_ = catalog.onboarding.onboard(account.id, draft)

    assert catalog.provisioning.calls == [(rt.id, rt.total_rooms) for rt in property_.room_types]
    assert [total for _, total in catalog.provisioning.calls] == [7, 2]
    [event] = catalog.events.of_type(PropertyOnboarded)
    snapshot = event.property
    assert snapshot.property_id == property_.id
    assert snapshot.owner_account_id == account.id
    assert snapshot.city == "Goa" and snapshot.locality == "Candolim"
    assert snapshot.pricing_strategy == PricingStrategyCode.WEEKEND
    assert [summary.name for summary in snapshot.room_types] == ["Deluxe", "Suite"]
    assert catalog.properties.get(property_.id).name == "Sea Breeze Resort"


def test_rule_violation_on_onboard_persists_nothing(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())

    with pytest.raises(BusinessRuleViolation):
        catalog.onboarding.onboard(account.id, property_draft(property_type=PropertyType.HOTEL, star_rating=None))

    assert catalog.properties.properties == {}
    assert catalog.provisioning.calls == []
    assert catalog.events.of_type(PropertyOnboarded) == []


def test_add_room_type_persists_provisions_and_publishes(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())
    property_ = catalog.onboarding.onboard(account.id, property_draft())

    added = catalog.room_types.add_room_type(property_.id, room_type_draft("Family Suite", total_rooms=3))

    assert [rt.name for rt in catalog.properties.get(property_.id).room_types] == ["Deluxe", "Family Suite"]
    assert catalog.provisioning.calls[-1] == (added.id, 3)
    [event] = catalog.events.of_type(RoomTypeAdded)
    assert event.room_type_id == added.id
    assert [summary.room_type_id for summary in event.property.room_types][-1] == added.id


def test_add_room_type_to_villa_is_rejected_before_any_side_effect(catalog):
    account = catalog.owner_accounts.open_account("owner-solo", owner_draft())
    villa = catalog.onboarding.onboard(account.id, villa_draft())
    provisioned = list(catalog.provisioning.calls)

    with pytest.raises(BusinessRuleViolation):
        catalog.room_types.add_room_type(villa.id, room_type_draft("Cottage", total_rooms=1))

    assert len(catalog.properties.get(villa.id).room_types) == 1
    assert catalog.provisioning.calls == provisioned
    assert catalog.events.of_type(RoomTypeAdded) == []


def test_add_room_type_to_unknown_property_is_not_found(catalog):
    with pytest.raises(NotFound) as error:
        catalog.room_types.add_room_type(uuid4(), room_type_draft())

    assert error.value.status_code == PROPERTY_NOT_FOUND


def test_room_type_snapshot_active_flags(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())
    property_ = catalog.onboarding.onboard(account.id, property_draft())
    room_type_id = property_.room_types[0].id

    active = catalog.queries.room_type_snapshot(room_type_id)
    catalog.properties.properties[property_.id].room_types[0].is_active = False
    room_type_inactive = catalog.queries.room_type_snapshot(room_type_id)
    catalog.properties.properties[property_.id].room_types[0].is_active = True
    catalog.properties.properties[property_.id].is_active = False
    property_inactive = catalog.queries.room_type_snapshot(room_type_id)

    assert active.is_active
    assert not room_type_inactive.is_active
    assert not property_inactive.is_active
    assert active.owner_account_id == account.id
    assert active.property_name == "Sea Breeze Resort" and active.room_type_name == "Deluxe"
    assert active.base_price == property_.room_types[0].base_price
    assert active.check_in_time == property_.check_in_time


def test_room_type_snapshot_of_unknown_room_type_is_not_found(catalog):
    with pytest.raises(NotFound) as error:
        catalog.queries.room_type_snapshot(uuid4())

    assert error.value.status_code == ROOM_TYPE_NOT_FOUND


def test_owner_user_id_lookup(catalog):
    account = catalog.owner_accounts.open_account("owner-1", owner_draft())

    assert catalog.queries.owner_user_id(account.id) == "owner-1"
    assert catalog.queries.owner_user_id(uuid4()) is None


def test_list_properties_of_unknown_account_is_not_found(catalog):
    with pytest.raises(NotFound):
        catalog.queries.list_properties(uuid4())


def test_list_amenities_is_ordered_by_category_then_code(catalog):
    codes = [amenity.code for amenity in catalog.queries.list_amenities()]

    assert codes[0] == "AIRPORT_SHUTTLE" and codes[-1] == "TV"
