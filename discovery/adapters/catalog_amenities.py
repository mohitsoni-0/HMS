"""``AmenityCatalogPort`` over ``CatalogAPI.known_amenity_codes`` (cached by the catalog)."""

from catalog.public.api import CatalogAPI
from discovery.interfaces.ports import AmenityCatalogPort


class CatalogAmenityAdapter(AmenityCatalogPort):
    def __init__(self, catalog: CatalogAPI) -> None:
        self._catalog = catalog

    def known_amenity_codes(self) -> frozenset[str]:
        return self._catalog.known_amenity_codes()
