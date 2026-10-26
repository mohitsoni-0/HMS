"""Model <-> domain mapping for the catalog. Django model instances never leave ``catalog.repositories``."""

from collections.abc import Iterable

from hotel_commons.domain.money import Money
from hotel_commons.models.base import RecordStatus

from catalog.domain.address import Address
from catalog.domain.owner_account import ContactDetails
from catalog.domain.owner_account import OwnerAccount as OwnerAccountEntity
from catalog.domain.property import Amenity as AmenityEntity
from catalog.domain.property import Property as PropertyEntity
from catalog.domain.room_type import RoomType as RoomTypeEntity
from catalog.models.catalog_models import Amenity, OwnerAccount, Property, RoomType


def _status(is_active: bool) -> RecordStatus:
    return RecordStatus.ACTIVE if is_active else RecordStatus.INACTIVE


def owner_account_to_model(account: OwnerAccountEntity) -> OwnerAccount:
    return OwnerAccount(
        id=account.id,
        display_name=account.display_name,
        owner_user_id=account.owner_user_id,
        contact_email=account.contact.email,
        contact_phone=account.contact.phone,
        status=_status(account.is_active),
    )


def owner_account_to_domain(row: OwnerAccount) -> OwnerAccountEntity:
    return OwnerAccountEntity(
        id=row.id,
        display_name=row.display_name,
        owner_user_id=row.owner_user_id,
        contact=ContactDetails(email=row.contact_email, phone=row.contact_phone),
        is_active=row.status == RecordStatus.ACTIVE,
        created_at=row.creation_date,
    )


def property_to_model(property_: PropertyEntity) -> Property:
    address = property_.address
    return Property(
        id=property_.id,
        owner_account_id=property_.owner_account_id,
        name=property_.name,
        description=property_.description,
        property_type=property_.property_type,
        star_rating=property_.star_rating,
        address_line1=address.line1,
        locality=address.locality,
        city=address.city,
        state=address.state,
        postal_code=address.postal_code,
        country=address.country,
        city_key=address.city_key,
        amenity_codes=sorted(property_.amenity_codes),
        pricing_strategy=property_.pricing_strategy,
        cancellation_policy=property_.cancellation_policy,
        check_in_time=property_.check_in_time,
        check_out_time=property_.check_out_time,
        status=_status(property_.is_active),
    )


def property_to_domain(row: Property, room_types: Iterable[RoomType]) -> PropertyEntity:
    return PropertyEntity(
        id=row.id,
        owner_account_id=row.owner_account_id,
        name=row.name,
        property_type=row.property_type,
        star_rating=row.star_rating,
        address=Address(
            line1=row.address_line1,
            locality=row.locality,
            city=row.city,
            state=row.state,
            postal_code=row.postal_code,
            country=row.country,
        ),
        amenity_codes=frozenset(row.amenity_codes),
        pricing_strategy=row.pricing_strategy,
        cancellation_policy=row.cancellation_policy,
        check_in_time=row.check_in_time,
        check_out_time=row.check_out_time,
        description=row.description,
        room_types=[room_type_to_domain(room_type) for room_type in room_types],
        is_active=row.status == RecordStatus.ACTIVE,
    )


def room_type_to_model(room_type: RoomTypeEntity) -> RoomType:
    return RoomType(
        id=room_type.id,
        property_id=room_type.property_id,
        name=room_type.name,
        description=room_type.description,
        max_occupancy=room_type.max_occupancy,
        total_rooms=room_type.total_rooms,
        base_price_paisa=room_type.base_price.paisa,
        currency=room_type.base_price.currency,
        amenity_codes=sorted(room_type.amenity_codes),
        status=_status(room_type.is_active),
    )


def room_type_to_domain(row: RoomType) -> RoomTypeEntity:
    return RoomTypeEntity(
        id=row.id,
        property_id=row.property_id,
        name=row.name,
        max_occupancy=row.max_occupancy,
        total_rooms=row.total_rooms,
        base_price=Money(row.base_price_paisa, row.currency),
        amenity_codes=frozenset(row.amenity_codes),
        description=row.description,
        is_active=row.status == RecordStatus.ACTIVE,
    )


def amenity_to_domain(row: Amenity) -> AmenityEntity:
    return AmenityEntity(code=row.code, name=row.name, category=row.category)
