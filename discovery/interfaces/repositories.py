"""Listing repository port (LLD §7.2)."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from discovery.domain.filters import Predicate
from discovery.domain.listing import Listing


class ListingRepository(ABC):
    @abstractmethod
    def upsert_many(self, listings: Sequence[Listing]) -> None:
        """Insert each listing, or replace the one already stored for its room type."""

    @abstractmethod
    def find(
        self, predicates: Sequence[Predicate], limit: int, after: tuple[UUID, UUID] | None = None
    ) -> list[Listing]:
        """At most ``limit`` listings matching every predicate, ordered by ``(property_id, room_type_id)``.

        ``after`` is the ``(property_id, room_type_id)`` of the last listing of the previous batch (keyset
        paging): only listings after it are returned, so a caller reads every match batch by batch.
        """
