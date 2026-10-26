"""Catalog reads (LLD §4.4) and the domain -> public snapshot mapping shared with the write services."""

import logging
import time
from collections.abc import Callable
from uuid import UUID

from hotel_commons.exceptions import NotFound

from catalog.constants import AMENITY_CODES_CACHE_SECONDS
from catalog.domain.owner_account import OwnerAccount
from catalog.domain.property import Amenity, Property
from catalog.domain.room_type import RoomType
from catalog.interfaces.repositories import AmenityRepository, OwnerAccountRepository, PropertyRepository
from catalog.public.dto import PropertySnapshot, RoomTypeSnapshot, RoomTypeSummary

logger = logging.getLogger(__name__)


class CatalogQueryService:
    def __init__(
        self,
        accounts: OwnerAccountRepository,
        properties: PropertyRepository,
        amenities: AmenityRepository,
        *,
        amenity_cache_seconds: float = AMENITY_CODES_CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._accounts = accounts
        self._properties = properties
        self._amenities = amenities
        self._amenity_cache_seconds = amenity_cache_seconds
        self._monotonic = monotonic
        self._amenity_codes: tuple[float, frozenset[str]] | None = None  # (read at, codes)

    def get_account(self, account_id: UUID) -> OwnerAccount:
        return self._accounts.get(account_id)

    def get_property(self, property_id: UUID) -> Property:
        return self._properties.get(property_id)

    def list_properties(self, account_id: UUID) -> list[Property]:
        """The account's properties; NotFound(OWNER_ACCOUNT_NOT_FOUND) for an unknown account."""
        account = self._accounts.get(account_id)
        return self._properties.list_for_account(account.id)

    def list_amenities(self) -> list[Amenity]:
        return self._amenities.list_all()

    def known_amenity_codes(self) -> frozenset[str]:
        """Codes of the active amenities, re-read at most every ``amenity_cache_seconds`` (search calls it a lot).

        The cache is one immutable tuple swapped in whole, so concurrent requests at worst both read the table.
        """
        cached, now = self._amenity_codes, self._monotonic()
        if cached is not None and now - cached[0] < self._amenity_cache_seconds:
            return cached[1]
        codes = self._amenities.known_codes()
        self._amenity_codes = (now, codes)
        return codes

    def room_type_snapshot(self, room_type_id: UUID) -> RoomTypeSnapshot:
        """What booking needs about a room type; NotFound(ROOM_TYPE_NOT_FOUND) when there is none."""
        property_, room_type = self._properties.get_room_type(room_type_id)
        return room_type_snapshot_of(property_, room_type)

    def owner_user_id(self, account_id: UUID) -> str | None:
        """The user owning the account, or None when there is no such account."""
        try:
            return self._accounts.get(account_id).owner_user_id
        except NotFound:
            logger.info("Owner account %s not found", account_id)
            return None


def property_snapshot(property_: Property) -> PropertySnapshot:
    return PropertySnapshot(
        property_id=property_.id,
        owner_account_id=property_.owner_account_id,
        name=property_.name,
        property_type=property_.property_type,
        star_rating=property_.star_rating,
        city=property_.address.city,
        locality=property_.address.locality,
        amenity_codes=property_.amenity_codes,
        pricing_strategy=property_.pricing_strategy,
        is_active=property_.is_active,
        room_types=tuple(_room_type_summary(room_type) for room_type in property_.room_types),
    )


def room_type_snapshot_of(property_: Property, room_type: RoomType) -> RoomTypeSnapshot:
    """A room type is sellable only while both it and its property are active."""
    return RoomTypeSnapshot(
        room_type_id=room_type.id,
        property_id=property_.id,
        owner_account_id=property_.owner_account_id,
        property_name=property_.name,
        room_type_name=room_type.name,
        max_occupancy=room_type.max_occupancy,
        total_rooms=room_type.total_rooms,
        base_price=room_type.base_price,
        pricing_strategy=property_.pricing_strategy,
        cancellation_policy=property_.cancellation_policy,
        check_in_time=property_.check_in_time,
        is_active=property_.is_active and room_type.is_active,
    )


def _room_type_summary(room_type: RoomType) -> RoomTypeSummary:
    return RoomTypeSummary(
        room_type_id=room_type.id,
        name=room_type.name,
        max_occupancy=room_type.max_occupancy,
        total_rooms=room_type.total_rooms,
        base_price=room_type.base_price,
        amenity_codes=room_type.amenity_codes,
        is_active=room_type.is_active,
    )
