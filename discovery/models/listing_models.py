"""The search read model in schema ``discovery`` (LLD §7.3, §3.15).

``Listing`` is a projection rebuilt from catalog events, one row per room type, so it keeps no log model
(HLD §8.3). Search reads it through two indexes: ``(city_key, is_active)`` for the mandatory city, and a GIN
index on ``amenity_codes`` for the ``@>`` (contains) amenity filter.
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from catalog.public.enums import PricingStrategyCode, PropertyType
from discovery.domain.listing import MAX_AMENITY_CODE_LENGTH
from hotel_commons.db.schema import schema_table
from hotel_commons.domain.money import DEFAULT_CURRENCY
from hotel_commons.models.base import AddCommonField
from hotel_commons.models.fields import EnumField


class Listing(AddCommonField):
    room_type_id = models.UUIDField(primary_key=True)
    property_id = models.UUIDField(db_index=True)
    property_name = models.CharField(max_length=200)
    property_type = EnumField(PropertyType)
    star_rating = models.SmallIntegerField(null=True, blank=True)
    city = models.CharField(max_length=100)
    city_key = models.CharField(max_length=100)
    locality = models.CharField(max_length=200)
    amenity_codes = ArrayField(models.CharField(max_length=MAX_AMENITY_CODE_LENGTH), default=list, blank=True)
    room_type_name = models.CharField(max_length=120)
    max_occupancy = models.SmallIntegerField()
    base_price_paisa = models.BigIntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    pricing_strategy = EnumField(PricingStrategyCode)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = schema_table("discovery", "listing")
        indexes = [
            models.Index(fields=["city_key", "is_active"], name="discovery_listing_city_idx"),
            GinIndex(fields=["amenity_codes"], name="discovery_listing_amenity_gin"),
        ]
