"""Search serializers (LLD §10.3).

``SearchQuerySerializer`` reads the query string (rupee amounts, comma lists, enum names) into a
``SearchCriteria``; ``SearchPageSerializer`` renders a ``SearchPage`` with amounts as rupee strings and a
sibling ``currency``.
"""

import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from rest_framework.exceptions import ValidationError

from catalog.public.enums import PricingStrategyCode, PropertyType
from discovery.domain.criteria import MAX_PAGE_SIZE, MAX_STAR_RATING, SearchCriteria, SortOrder
from discovery.domain.listing import MAX_AMENITY_CODE_LENGTH
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import (
    HotelAmountField,
    HotelCharField,
    HotelCsvListField,
    HotelDateField,
    HotelEnumField,
    HotelIntegerField,
    HotelListField,
    HotelUUIDField,
)

MAX_GUESTS = 40
MAX_ROOMS = 10


class SearchQuerySerializer(BaseHotelSerializer):
    city = HotelCharField(max_length=100)
    check_in = HotelDateField()
    check_out = HotelDateField(help_text="Check-out day (exclusive), after check_in.")
    guests = HotelIntegerField(min_value=1, max_value=MAX_GUESTS)
    rooms = HotelIntegerField(min_value=1, max_value=MAX_ROOMS, default=1)
    locality = HotelCharField(required=False, max_length=200)
    min_price = HotelAmountField(required=False, min_rupees=Decimal("0"), help_text="Minimum average nightly price.")
    max_price = HotelAmountField(required=False, min_rupees=Decimal("0"), help_text="Maximum average nightly price.")
    amenities = HotelCsvListField(
        child=HotelCharField(max_length=MAX_AMENITY_CODE_LENGTH),
        required=False,
        help_text="Comma-separated amenity codes, all required, e.g. WIFI,POOL.",
    )
    min_rating = HotelIntegerField(required=False, min_value=1, max_value=MAX_STAR_RATING)
    property_types = HotelCsvListField(
        child=HotelEnumField(PropertyType), required=False, help_text="Comma-separated property types."
    )
    sort = HotelEnumField(SortOrder, label_name="label", default=SortOrder.PRICE_LOW_TO_HIGH)
    page = HotelIntegerField(min_value=1, default=1)
    page_size = HotelIntegerField(min_value=1, max_value=MAX_PAGE_SIZE, default=20)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        errors = {**_stay_errors(attrs["check_in"], attrs["check_out"]), **_price_errors(attrs)}
        if errors:
            raise ValidationError(errors)
        return attrs

    def to_criteria(self) -> SearchCriteria:
        data = self.validated_data
        return SearchCriteria(
            city=data["city"],
            stay=DateRange(data["check_in"], data["check_out"]),
            guests=data["guests"],
            rooms=data["rooms"],
            locality=data.get("locality") or None,
            min_price=_money(data.get("min_price")),
            max_price=_money(data.get("max_price")),
            amenities=frozenset(code.upper() for code in data.get("amenities", ())),
            min_star_rating=data.get("min_rating"),
            property_types=frozenset(data.get("property_types", ())),
            sort=data["sort"],
            page=data["page"],
            page_size=data["page_size"],
        )


def _stay_errors(check_in: datetime.date, check_out: datetime.date) -> dict[str, str]:
    if check_out <= check_in:
        return {"check_out": "check_out must be after check_in."}
    if (check_out - check_in).days > settings.MAX_STAY_NIGHTS:
        return {"check_out": f"A stay can be at most {settings.MAX_STAY_NIGHTS} nights."}
    return {}


def _price_errors(attrs: dict[str, Any]) -> dict[str, str]:
    min_price, max_price = attrs.get("min_price"), attrs.get("max_price")
    if min_price is not None and max_price is not None and min_price > max_price:
        return {"max_price": "max_price must not be below min_price."}
    return {}


def _money(paisa: int | None) -> Money | None:
    return None if paisa is None else Money(paisa)


class NightlyRateSerializer(BaseHotelSerializer):
    """One ``(night, price)`` pair of a quote."""

    date = HotelDateField()
    price = HotelAmountField()

    def to_representation(self, instance: tuple[datetime.date, Money]) -> dict[str, Any]:
        night, price = instance
        return super().to_representation({"date": night, "price": price})


class SortedCodeListField(HotelListField):
    """A set of codes rendered as a sorted list, so responses are stable."""

    def to_representation(self, data: Any) -> list[Any]:
        return super().to_representation(sorted(data))


class RoomOfferSerializer(BaseHotelSerializer):
    room_type_id = HotelUUIDField(source="listing.room_type_id")
    room_type = HotelCharField(source="listing.room_type_name")
    max_occupancy = HotelIntegerField(source="listing.max_occupancy")
    available_rooms = HotelIntegerField()
    pricing_strategy = HotelEnumField(PricingStrategyCode, source="quote.strategy")
    nightly_rates = NightlyRateSerializer(many=True, source="quote.nightly_rates")
    total = HotelAmountField(source="quote.total", help_text="Price of all rooms for the whole stay.")
    average_nightly = HotelAmountField(source="quote.average_nightly", help_text="Per room, per night.")
    currency = HotelCharField(source="quote.total.currency")


class PropertyResultSerializer(BaseHotelSerializer):
    property_id = HotelUUIDField()
    name = HotelCharField(source="property_name")
    property_type = HotelEnumField(PropertyType)
    star_rating = HotelIntegerField(allow_null=True)
    city = HotelCharField()
    locality = HotelCharField()
    amenities = SortedCodeListField(child=HotelCharField(), source="amenity_codes")
    lowest_nightly_price = HotelAmountField()
    currency = HotelCharField(source="lowest_nightly_price.currency")
    offers = RoomOfferSerializer(many=True)


class SearchPageSerializer(BaseHotelSerializer):
    items = PropertyResultSerializer(many=True)
    page = HotelIntegerField()
    page_size = HotelIntegerField()
    total = HotelIntegerField()
