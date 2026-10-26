"""Catalog tables in schema ``catalog`` (LLD §4.6), as ``CommonX`` / ``X`` / ``XLog`` triads (LLD §3.5).

Unique constraints, CHECKs and indexes live on the live tables only, so log snapshots never collide. Log tables
get a ``(log, creation_date)`` index for an entity's history and a BRIN index on ``creation_date`` (HLD §8).
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from hotel_commons.db.schema import schema_table
from hotel_commons.models.base import AddCommonField
from hotel_commons.models.fields import BlindIndexField, EncryptedTextField, EnumField
from hotel_commons.models.managers import CustomUpdateManager
from hotel_commons.security.pi_type import PiType
from uuid6 import uuid7

from catalog.constants import (
    ADDRESS_LINE_MAX_LENGTH,
    ADDRESS_PART_MAX_LENGTH,
    AMENITY_CODE_MAX_LENGTH,
    AMENITY_NAME_MAX_LENGTH,
    DEFAULT_CHECK_IN_TIME,
    DEFAULT_CHECK_OUT_TIME,
    DEFAULT_COUNTRY,
    MAX_OCCUPANCY_MAX,
    MAX_OCCUPANCY_MIN,
    OWNER_DISPLAY_NAME_MAX_LENGTH,
    OWNER_USER_ID_MAX_LENGTH,
    PROPERTY_NAME_MAX_LENGTH,
    ROOM_TYPE_NAME_MAX_LENGTH,
    SCHEMA,
    STAR_RATING_MAX,
    STAR_RATING_MIN,
    TOTAL_ROOMS_MIN,
)
from catalog.enums import AmenityCategory, CancellationPolicyCode, PricingStrategyCode, PropertyType

MASKED_VALUE_MAX_LENGTH = 255
POSTAL_CODE_MAX_LENGTH = 10
COUNTRY_CODE_LENGTH = 2
CURRENCY_CODE_LENGTH = 3


def _amenity_codes() -> ArrayField:
    return ArrayField(models.CharField(max_length=AMENITY_CODE_MAX_LENGTH), default=list, blank=True)


# --- owner_account ------------------------------------------------------------------------------------------


class CommonOwnerAccount(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    display_name = models.CharField(max_length=OWNER_DISPLAY_NAME_MAX_LENGTH)
    owner_user_id = models.CharField(max_length=OWNER_USER_ID_MAX_LENGTH)
    contact_email = EncryptedTextField(pi_type=PiType.EMAIL)
    contact_email_hash = BlindIndexField(db_index=False)  # the live table's UNIQUE constraint is its index
    masked_contact_email = models.CharField(max_length=MASKED_VALUE_MAX_LENGTH, null=True, blank=True)
    contact_phone = EncryptedTextField(pi_type=PiType.PHONE)
    masked_contact_phone = models.CharField(max_length=MASKED_VALUE_MAX_LENGTH, null=True, blank=True)

    class Meta:
        abstract = True


class OwnerAccount(CommonOwnerAccount):
    CustomUpdateManager.set_logModel(logModel="catalog.OwnerAccountLog", model="catalog.OwnerAccount")

    class Meta:
        db_table = schema_table(SCHEMA, "owner_account")
        indexes = [models.Index(fields=["owner_user_id"], name="catalog_owner_acc_user_idx")]
        constraints = [
            models.UniqueConstraint(fields=["contact_email_hash"], name="catalog_owner_account_email_hash_uniq")
        ]


class OwnerAccountLog(CommonOwnerAccount):
    log = models.ForeignKey(OwnerAccount, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table(SCHEMA, "owner_account_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="catalog_owner_acc_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="catalog_owner_acc_log_brin"),
        ]


# --- property -----------------------------------------------------------------------------------------------


class CommonProperty(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    owner_account = models.ForeignKey(
        "catalog.OwnerAccount", on_delete=models.RESTRICT, related_name="+", db_index=False
    )
    name = models.CharField(max_length=PROPERTY_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    property_type = EnumField(PropertyType)
    star_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    address_line1 = models.CharField(max_length=ADDRESS_LINE_MAX_LENGTH)
    locality = models.CharField(max_length=ADDRESS_PART_MAX_LENGTH)
    city = models.CharField(max_length=ADDRESS_PART_MAX_LENGTH)
    state = models.CharField(max_length=ADDRESS_PART_MAX_LENGTH)
    postal_code = models.CharField(max_length=POSTAL_CODE_MAX_LENGTH)
    country = models.CharField(max_length=COUNTRY_CODE_LENGTH, default=DEFAULT_COUNTRY)
    city_key = models.CharField(max_length=ADDRESS_PART_MAX_LENGTH)
    amenity_codes = _amenity_codes()
    pricing_strategy = EnumField(PricingStrategyCode)
    cancellation_policy = EnumField(CancellationPolicyCode)
    check_in_time = models.TimeField(default=DEFAULT_CHECK_IN_TIME)
    check_out_time = models.TimeField(default=DEFAULT_CHECK_OUT_TIME)

    class Meta:
        abstract = True


class Property(CommonProperty):
    CustomUpdateManager.set_logModel(logModel="catalog.PropertyLog", model="catalog.Property")

    class Meta:
        db_table = schema_table(SCHEMA, "property")
        indexes = [
            models.Index(fields=["owner_account"], name="catalog_property_owner_idx"),
            models.Index(fields=["city_key"], name="catalog_property_city_key_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(star_rating__isnull=True)
                | Q(star_rating__gte=STAR_RATING_MIN, star_rating__lte=STAR_RATING_MAX),
                name="catalog_property_star_rating_valid",
            )
        ]


class PropertyLog(CommonProperty):
    log = models.ForeignKey(Property, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table(SCHEMA, "property_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="catalog_property_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="catalog_property_log_brin"),
        ]


# --- room_type ----------------------------------------------------------------------------------------------


class CommonRoomType(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    property = models.ForeignKey("catalog.Property", on_delete=models.RESTRICT, related_name="+", db_index=False)
    name = models.CharField(max_length=ROOM_TYPE_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    max_occupancy = models.PositiveSmallIntegerField()
    total_rooms = models.PositiveSmallIntegerField()
    base_price_paisa = models.BigIntegerField(db_comment="Base nightly price in paisa (rupees x 100)")
    currency = models.CharField(max_length=CURRENCY_CODE_LENGTH, default="INR")
    amenity_codes = _amenity_codes()

    class Meta:
        abstract = True


class RoomType(CommonRoomType):
    CustomUpdateManager.set_logModel(logModel="catalog.RoomTypeLog", model="catalog.RoomType")

    # Overrides the abstract FK to give Property a ``room_types`` reverse accessor (prefetching); the unique
    # (property, lower(name)) index below leads with property_id, so it also serves lookups by property.
    property = models.ForeignKey(Property, on_delete=models.RESTRICT, related_name="room_types", db_index=False)

    class Meta:
        db_table = schema_table(SCHEMA, "room_type")
        constraints = [
            models.UniqueConstraint(F("property"), Lower("name"), name="catalog_room_type_property_name_uniq"),
            models.CheckConstraint(
                condition=Q(max_occupancy__gte=MAX_OCCUPANCY_MIN, max_occupancy__lte=MAX_OCCUPANCY_MAX),
                name="catalog_room_type_max_occupancy_valid",
            ),
            models.CheckConstraint(
                condition=Q(total_rooms__gte=TOTAL_ROOMS_MIN), name="catalog_room_type_total_rooms_valid"
            ),
            models.CheckConstraint(condition=Q(base_price_paisa__gt=0), name="catalog_room_type_base_price_positive"),
        ]


class RoomTypeLog(CommonRoomType):
    log = models.ForeignKey(RoomType, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table(SCHEMA, "room_type_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="catalog_room_type_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="catalog_room_type_log_brin"),
        ]


# --- amenity (seed data, no log) ----------------------------------------------------------------------------


class Amenity(AddCommonField):
    code = models.CharField(primary_key=True, max_length=AMENITY_CODE_MAX_LENGTH)
    name = models.CharField(max_length=AMENITY_NAME_MAX_LENGTH)
    category = EnumField(AmenityCategory)

    class Meta:
        db_table = schema_table(SCHEMA, "amenity")
