"""``QuotePort`` over ``PricingAPI.quote_many``, mapping pricing's ``QuoteDTO`` to discovery's ``OfferQuote``."""

from collections.abc import Sequence
from uuid import UUID

from discovery.domain.listing import Listing
from discovery.domain.offers import OfferQuote
from discovery.interfaces.ports import QuotePort
from hotel_commons.domain.date_range import DateRange
from pricing.public.api import PricingAPI
from pricing.public.dto import PricingRequest, QuoteDTO


class PricingQuoteAdapter(QuotePort):
    def __init__(self, pricing: PricingAPI) -> None:
        self._pricing = pricing

    def quote_many(self, listings: Sequence[Listing], stay: DateRange, rooms: int) -> dict[UUID, OfferQuote]:
        requests = [
            PricingRequest(
                room_type_id=listing.room_type_id,
                base_price=listing.base_price,
                strategy=listing.pricing_strategy,
                stay=stay,
                rooms=rooms,
            )
            for listing in listings
        ]
        return {room_type_id: _offer_quote(dto) for room_type_id, dto in self._pricing.quote_many(requests).items()}


def _offer_quote(dto: QuoteDTO) -> OfferQuote:
    return OfferQuote(
        strategy=dto.strategy,
        nightly_rates=dto.nightly_rates,
        total=dto.total,
        average_nightly=dto.average_nightly,
    )
