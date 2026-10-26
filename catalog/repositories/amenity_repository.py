"""Django ``AmenityRepository`` (LLD §4.3). Amenities are seed data (migration 0002_seed_amenities)."""

from catalog.domain.property import Amenity as AmenityEntity
from catalog.interfaces.repositories import AmenityRepository
from catalog.models.catalog_models import Amenity
from catalog.repositories.mappers import amenity_to_domain


class DjangoAmenityRepository(AmenityRepository):
    def known_codes(self) -> frozenset[str]:
        return frozenset(Amenity.objects_active.values_list("code", flat=True))

    def list_all(self) -> list[AmenityEntity]:
        return [amenity_to_domain(row) for row in Amenity.objects_active.order_by("category", "code")]
