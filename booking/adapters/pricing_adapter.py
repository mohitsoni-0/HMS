"""``PricingPort`` over the pricing public API (LLD §8.3): ``QuoteDTO`` -> ``BookingPrice``."""

from hotel_commons.domain.date_range import DateRange

from booking.domain.value_objects import BookingPrice, RoomTypeInfo
from booking.interfaces.ports import PricingPort
from pricing.public.api import get_pricing_api
from pricing.public.dto import PricingRequest


class PricingAdapter(PricingPort):
    def quote(self, room_type: RoomTypeInfo, stay: DateRange, rooms: int) -> BookingPrice:
        quote = get_pricing_api().quote(
            PricingRequest(
                room_type_id=room_type.room_type_id,
                base_price=room_type.base_price,
                strategy=room_type.pricing_strategy,
                stay=stay,
                rooms=rooms,
            )
        )
        return BookingPrice(
            nightly_rates=quote.nightly_rates, rooms=quote.rooms, total=quote.total, pricing_strategy=quote.strategy
        )
