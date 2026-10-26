"""``CatalogAPI``: the facade other contexts call (LLD §4.5).

The module imports no Django; ``get_catalog_api`` reaches the container only when it is called.
"""

from uuid import UUID

from catalog.public.dto import RoomTypeSnapshot
from catalog.services.query_service import CatalogQueryService


class CatalogAPI:
    def __init__(self, queries: CatalogQueryService) -> None:
        self._queries = queries

    def room_type_snapshot(self, room_type_id: UUID) -> RoomTypeSnapshot:
        """NotFound(ROOM_TYPE_NOT_FOUND) when there is no such room type."""
        return self._queries.room_type_snapshot(room_type_id)

    def owner_user_id(self, account_id: UUID) -> str | None:
        """The user owning the account, or None when there is no such account."""
        return self._queries.owner_user_id(account_id)

    def known_amenity_codes(self) -> frozenset[str]:
        """Codes of the active amenities (cached for ``AMENITY_CODES_CACHE_SECONDS``)."""
        return self._queries.known_amenity_codes()


def get_catalog_api() -> CatalogAPI:
    from catalog import container

    return CatalogAPI(container.query_service())
