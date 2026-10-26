"""Catalog constants: field limits shared by the domain, models and serializers, defaults, and the amenity
seed (LLD §4.2, §4.6, §10.3). Pure values, importable without Django."""

from datetime import time
from typing import Final

from catalog.enums import AmenityCategory

SCHEMA: Final = "catalog"

DEFAULT_COUNTRY: Final = "IN"
DEFAULT_CHECK_IN_TIME: Final = time(14, 0)
DEFAULT_CHECK_OUT_TIME: Final = time(11, 0)

OWNER_DISPLAY_NAME_MIN_LENGTH: Final = 2
OWNER_DISPLAY_NAME_MAX_LENGTH: Final = 200
OWNER_USER_ID_MAX_LENGTH: Final = 64

PROPERTY_NAME_MIN_LENGTH: Final = 2
PROPERTY_NAME_MAX_LENGTH: Final = 200
STAR_RATING_MIN: Final = 1
STAR_RATING_MAX: Final = 5

ROOM_TYPE_NAME_MIN_LENGTH: Final = 2
ROOM_TYPE_NAME_MAX_LENGTH: Final = 120
MAX_OCCUPANCY_MIN: Final = 1
MAX_OCCUPANCY_MAX: Final = 10
TOTAL_ROOMS_MIN: Final = 1
TOTAL_ROOMS_MAX: Final = 500
MAX_ROOM_TYPES_PER_REQUEST: Final = 50

ADDRESS_LINE_MAX_LENGTH: Final = 200
ADDRESS_PART_MAX_LENGTH: Final = 100
POSTAL_CODE_PATTERN: Final = r"[0-9]{6}"

AMENITY_CODE_MAX_LENGTH: Final = 40
AMENITY_NAME_MAX_LENGTH: Final = 100
# How long ``CatalogQueryService.known_amenity_codes`` reuses the codes it read (they are seed data).
AMENITY_CODES_CACHE_SECONDS: Final = 300.0

# (code, name, category) of the amenities seeded by migration 0002_seed_amenities (LLD §4.6).
AMENITY_SEED: Final[tuple[tuple[str, str, AmenityCategory], ...]] = (
    ("WIFI", "Free Wi-Fi", AmenityCategory.PROPERTY),
    ("PARKING", "Parking", AmenityCategory.PROPERTY),
    ("POOL", "Swimming pool", AmenityCategory.PROPERTY),
    ("GYM", "Gym", AmenityCategory.PROPERTY),
    ("SPA", "Spa", AmenityCategory.PROPERTY),
    ("RESTAURANT", "Restaurant", AmenityCategory.PROPERTY),
    ("BREAKFAST", "Breakfast included", AmenityCategory.PROPERTY),
    ("AIR_CONDITIONING", "Air conditioning", AmenityCategory.ROOM),
    ("PET_FRIENDLY", "Pet friendly", AmenityCategory.PROPERTY),
    ("AIRPORT_SHUTTLE", "Airport shuttle", AmenityCategory.PROPERTY),
    ("TV", "Television", AmenityCategory.ROOM),
    ("MINIBAR", "Minibar", AmenityCategory.ROOM),
    ("BALCONY", "Balcony", AmenityCategory.ROOM),
    ("KITCHEN", "Kitchen", AmenityCategory.ROOM),
)
