"""Listing repository port (LLD §7.2)."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from discovery.domain.filters import Predicate
from discovery.domain.listing import Listing


class ListingRepository(ABC):
    @abstractmethod
    def upsert_many(self, listings: Sequence[Listing]) -> None:
        """Insert each listing, or replace the one already stored for its room type."""

    @abstractmethod
    def find(self, predicates: Sequence[Predicate], limit: int) -> list[Listing]:
        """At most ``limit`` listings matching every predicate, in a stable order."""
