"""``CatalogPort`` over the catalog's public API (LLD §8.3): ``RoomTypeSnapshot`` -> ``RoomTypeInfo``."""

from uuid import UUID

from catalog.public.api import get_catalog_api

from booking.domain.value_objects import RoomTypeInfo
from booking.interfaces.ports import CatalogPort


class CatalogAdapter(CatalogPort):
    def room_type_info(self, room_type_id: UUID) -> RoomTypeInfo:
        snapshot = get_catalog_api().room_type_snapshot(room_type_id)
        return RoomTypeInfo(
            room_type_id=snapshot.room_type_id,
            property_id=snapshot.property_id,
            owner_account_id=snapshot.owner_account_id,
            max_occupancy=snapshot.max_occupancy,
            total_rooms=snapshot.total_rooms,
            base_price=snapshot.base_price,
            pricing_strategy=snapshot.pricing_strategy,
            cancellation_policy=snapshot.cancellation_policy,
            check_in_time=snapshot.check_in_time,
            is_active=snapshot.is_active,
        )
