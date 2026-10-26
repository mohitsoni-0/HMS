"""Property aggregate: onboarding validation, property-type rules and room-type additions (LLD §4.2)."""

from uuid import uuid4

import pytest
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation, Conflict, ValidationFailed

from catalog.domain.owner_account import ContactDetails
from catalog.domain.property import Property
from catalog.domain.property_rules import DEFAULT_PROPERTY_RULES
from catalog.enums import PropertyType
from catalog.status_code import (
    DUPLICATE_ROOM_TYPE_NAME,
    INVALID_CONTACT,
    INVALID_PROPERTY,
    PROPERTY_TYPE_RULE_VIOLATED,
    ROOM_TYPE_REQUIRED,
    UNKNOWN_AMENITIES,
)
from catalog.tests.fakes import KNOWN_AMENITIES, property_draft, room_type_draft, villa_draft


def onboard(draft):
    return Property.onboard(
        owner_account_id=uuid4(), draft=draft, known_amenities=KNOWN_AMENITIES, rules=DEFAULT_PROPERTY_RULES
    )


def test_onboard_builds_property_with_room_types():
    property_ = onboard(property_draft(room_types=(room_type_draft("Deluxe"), room_type_draft("Suite"))))

    assert [room_type.name for room_type in property_.room_types] == ["Deluxe", "Suite"]
    assert {room_type.property_id for room_type in property_.room_types} == {property_.id}
    assert property_.room_types[0].base_price == Money(480_000)
    assert property_.is_active
    assert property_.address.city_key == "goa"


def test_onboard_requires_room_type():
    with pytest.raises(ValidationFailed) as error:
        onboard(property_draft(room_types=()))

    assert error.value.status_code == ROOM_TYPE_REQUIRED


def test_hotel_requires_star_rating():
    with pytest.raises(BusinessRuleViolation) as error:
        onboard(property_draft(property_type=PropertyType.HOTEL, star_rating=None))

    assert error.value.status_code == PROPERTY_TYPE_RULE_VIOLATED
    assert "star rating" in error.value.message


def test_homestay_needs_no_star_rating():
    assert onboard(property_draft(property_type=PropertyType.HOMESTAY, star_rating=None)).star_rating is None


@pytest.mark.parametrize("star_rating", [0, 6, True])
def test_star_rating_out_of_range_is_invalid(star_rating):
    with pytest.raises(ValidationFailed) as error:
        onboard(property_draft(star_rating=star_rating))

    assert error.value.status_code == INVALID_PROPERTY


@pytest.mark.parametrize(
    "overrides",
    [{"max_occupancy": 0}, {"max_occupancy": 11}, {"total_rooms": 0}, {"total_rooms": 501}, {"base_price": Money(0)}],
)
def test_room_type_limits(overrides):
    with pytest.raises(ValidationFailed) as error:
        onboard(property_draft(room_types=(room_type_draft(**overrides),)))

    assert error.value.status_code == INVALID_PROPERTY


def test_villa_must_be_a_single_entire_place():
    with pytest.raises(BusinessRuleViolation) as error:
        onboard(villa_draft(room_types=(room_type_draft("Entire Villa", total_rooms=2),)))

    assert error.value.status_code == PROPERTY_TYPE_RULE_VIOLATED


def test_villa_entire_place_rule_on_add_room_type():
    villa = onboard(villa_draft())

    with pytest.raises(BusinessRuleViolation) as error:
        villa.add_room_type(room_type_draft("Garden Cottage", total_rooms=1), KNOWN_AMENITIES, DEFAULT_PROPERTY_RULES)

    assert error.value.status_code == PROPERTY_TYPE_RULE_VIOLATED
    assert [room_type.name for room_type in villa.room_types] == ["Entire Villa"]


def test_add_room_type_appends_and_returns_it():
    resort = onboard(property_draft())

    added = resort.add_room_type(room_type_draft("Family Suite"), KNOWN_AMENITIES, DEFAULT_PROPERTY_RULES)

    assert resort.room_types[-1] is added
    assert added.property_id == resort.id


def test_duplicate_room_type_name_case_insensitive():
    with pytest.raises(Conflict) as on_onboard:
        onboard(property_draft(room_types=(room_type_draft("Deluxe"), room_type_draft("DELUXE "))))
    resort = onboard(property_draft(room_types=(room_type_draft("Deluxe Sea View"),)))
    with pytest.raises(Conflict) as on_add:
        resort.add_room_type(room_type_draft("deluxe sea view"), KNOWN_AMENITIES, DEFAULT_PROPERTY_RULES)

    assert on_onboard.value.status_code == DUPLICATE_ROOM_TYPE_NAME
    assert on_add.value.status_code == DUPLICATE_ROOM_TYPE_NAME
    assert len(resort.room_types) == 1


def test_unknown_amenities_rejected():
    with pytest.raises(ValidationFailed) as on_property:
        onboard(property_draft(amenity_codes=frozenset({"WIFI", "HELIPAD"})))
    with pytest.raises(ValidationFailed) as on_room_type:
        onboard(property_draft(room_types=(room_type_draft(amenity_codes=frozenset({"JACUZZI", "BIDET"})),)))

    assert on_property.value.status_code == UNKNOWN_AMENITIES
    assert on_property.value.message == "Unknown amenity codes: HELIPAD"
    assert on_room_type.value.message == "Unknown amenity codes: BIDET, JACUZZI"


@pytest.mark.parametrize("name", ["", "A", "x" * 201])
def test_property_name_length(name):
    with pytest.raises(ValidationFailed) as error:
        onboard(property_draft(name=name))

    assert error.value.status_code == INVALID_PROPERTY


@pytest.mark.parametrize(("email", "phone"), [("not-an-email", "9876543210"), ("a@example.com", "5876543210")])
def test_contact_details_are_validated_without_echoing_them(email, phone):
    with pytest.raises(ValidationFailed) as error:
        ContactDetails(email=email, phone=phone)

    assert error.value.status_code == INVALID_CONTACT
    assert email not in error.value.message and phone not in error.value.message


def test_contact_details_repr_hides_pii():
    contact = ContactDetails(email="owner@example.com", phone="9876543210")

    assert "owner@example.com" not in repr(contact)
    assert "9876543210" not in repr(contact)
