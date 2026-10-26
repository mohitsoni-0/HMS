"""Property, room-type and amenity serializers (LLD §10.3).

Amounts are rupee strings on the wire and paisa inside (``HotelAmountField``); enums are member names.
"""

from decimal import Decimal
from typing import Any

from django.core.validators import RegexValidator
from hotel_commons.domain.money import Money
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import (
    HotelAmountField,
    HotelBooleanField,
    HotelCharField,
    HotelEnumField,
    HotelIntegerField,
    HotelListField,
    HotelTimeField,
    HotelUUIDField,
)

from catalog.constants import (
    ADDRESS_LINE_MAX_LENGTH,
    ADDRESS_PART_MAX_LENGTH,
    AMENITY_CODE_MAX_LENGTH,
    DEFAULT_CHECK_IN_TIME,
    DEFAULT_CHECK_OUT_TIME,
    DEFAULT_COUNTRY,
    MAX_OCCUPANCY_MAX,
    MAX_OCCUPANCY_MIN,
    MAX_ROOM_TYPES_PER_REQUEST,
    POSTAL_CODE_PATTERN,
    PROPERTY_NAME_MAX_LENGTH,
    PROPERTY_NAME_MIN_LENGTH,
    ROOM_TYPE_NAME_MAX_LENGTH,
    ROOM_TYPE_NAME_MIN_LENGTH,
    STAR_RATING_MAX,
    STAR_RATING_MIN,
    TOTAL_ROOMS_MAX,
    TOTAL_ROOMS_MIN,
)
from catalog.domain.address import Address
from catalog.enums import AmenityCategory, CancellationPolicyCode, PricingStrategyCode, PropertyType
from catalog.services.commands import PropertyDraft, RoomTypeDraft

MIN_BASE_PRICE_RUPEES = Decimal("1")


class AmenityCodesField(HotelListField):
    """Amenity codes: a JSON list of strings in, a sorted list out (the domain keeps a frozenset)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(child=HotelCharField(max_length=AMENITY_CODE_MAX_LENGTH), **kwargs)

    def to_representation(self, data: Any) -> list[str]:
        return sorted(super().to_representation(data))


class AmenitySerializer(BaseHotelSerializer):
    code = HotelCharField()
    name = HotelCharField()
    category = HotelEnumField(AmenityCategory)


class AddressSerializer(BaseHotelSerializer):
    line1 = HotelCharField(max_length=ADDRESS_LINE_MAX_LENGTH)
    locality = HotelCharField(max_length=ADDRESS_PART_MAX_LENGTH)
    city = HotelCharField(max_length=ADDRESS_PART_MAX_LENGTH)
    state = HotelCharField(max_length=ADDRESS_PART_MAX_LENGTH)
    postal_code = HotelCharField(validators=[RegexValidator(f"^{POSTAL_CODE_PATTERN}$", "Enter a 6-digit PIN code.")])
    country = HotelCharField(min_length=2, max_length=2, default=DEFAULT_COUNTRY)


class RoomTypeCreateSerializer(BaseHotelSerializer):
    name = HotelCharField(min_length=ROOM_TYPE_NAME_MIN_LENGTH, max_length=ROOM_TYPE_NAME_MAX_LENGTH)
    description = HotelCharField(allow_blank=True, default="")
    max_occupancy = HotelIntegerField(min_value=MAX_OCCUPANCY_MIN, max_value=MAX_OCCUPANCY_MAX)
    total_rooms = HotelIntegerField(min_value=TOTAL_ROOMS_MIN, max_value=TOTAL_ROOMS_MAX)
    base_price = HotelAmountField(min_rupees=MIN_BASE_PRICE_RUPEES, help_text="Base nightly price in rupees.")
    amenities = AmenityCodesField(default=list)

    def to_draft(self) -> RoomTypeDraft:
        return room_type_draft(self.validated_data)


class PropertyOnboardSerializer(BaseHotelSerializer):
    name = HotelCharField(min_length=PROPERTY_NAME_MIN_LENGTH, max_length=PROPERTY_NAME_MAX_LENGTH)
    description = HotelCharField(allow_blank=True, default="")
    property_type = HotelEnumField(PropertyType)
    star_rating = HotelIntegerField(min_value=STAR_RATING_MIN, max_value=STAR_RATING_MAX, allow_null=True, default=None)
    address = AddressSerializer()
    amenities = AmenityCodesField(default=list)
    pricing_strategy = HotelEnumField(PricingStrategyCode, default=PricingStrategyCode.STANDARD)
    cancellation_policy = HotelEnumField(CancellationPolicyCode, default=CancellationPolicyCode.MODERATE)
    check_in_time = HotelTimeField(default=DEFAULT_CHECK_IN_TIME)
    check_out_time = HotelTimeField(default=DEFAULT_CHECK_OUT_TIME)
    room_types = RoomTypeCreateSerializer(many=True, min_length=1, max_length=MAX_ROOM_TYPES_PER_REQUEST)

    def to_draft(self) -> PropertyDraft:
        data = self.validated_data
        return PropertyDraft(
            name=data["name"],
            property_type=data["property_type"],
            star_rating=data["star_rating"],
            address=Address(**data["address"]),
            amenity_codes=frozenset(data["amenities"]),
            pricing_strategy=data["pricing_strategy"],
            cancellation_policy=data["cancellation_policy"],
            check_in_time=data["check_in_time"],
            check_out_time=data["check_out_time"],
            description=data["description"],
            room_types=tuple(room_type_draft(room_type) for room_type in data["room_types"]),
        )


class RoomTypeSerializer(BaseHotelSerializer):
    room_type_id = HotelUUIDField(source="id")
    name = HotelCharField()
    description = HotelCharField()
    max_occupancy = HotelIntegerField()
    total_rooms = HotelIntegerField()
    base_price = HotelAmountField()
    currency = HotelCharField(source="base_price.currency")
    amenities = AmenityCodesField(source="amenity_codes")
    is_active = HotelBooleanField()


class PropertySerializer(BaseHotelSerializer):
    property_id = HotelUUIDField(source="id")
    owner_account_id = HotelUUIDField()
    name = HotelCharField()
    description = HotelCharField()
    property_type = HotelEnumField(PropertyType)
    star_rating = HotelIntegerField(allow_null=True)
    address = AddressSerializer()
    amenities = AmenityCodesField(source="amenity_codes")
    pricing_strategy = HotelEnumField(PricingStrategyCode)
    cancellation_policy = HotelEnumField(CancellationPolicyCode)
    check_in_time = HotelTimeField()
    check_out_time = HotelTimeField()
    is_active = HotelBooleanField()
    room_types = RoomTypeSerializer(many=True)


def room_type_draft(data: dict[str, Any]) -> RoomTypeDraft:
    """A ``RoomTypeDraft`` from validated ``RoomTypeCreateSerializer`` data (``base_price`` is already paisa)."""
    return RoomTypeDraft(
        name=data["name"],
        max_occupancy=data["max_occupancy"],
        total_rooms=data["total_rooms"],
        base_price=Money(data["base_price"]),
        amenity_codes=frozenset(data["amenities"]),
        description=data["description"],
    )
