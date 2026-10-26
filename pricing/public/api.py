"""Pricing facade for other contexts (LLD §6): quotes as DTOs, never domain objects."""

from collections.abc import Sequence
from uuid import UUID

from pricing.container import pricing_service
from pricing.domain.quote import PriceQuote
from pricing.public.dto import PricingRequest, QuoteDTO
from pricing.services.pricing_service import PricingService


class PricingAPI:
    def __init__(self, service: PricingService) -> None:
        self._service = service

    def quote(self, request: PricingRequest) -> QuoteDTO:
        """Price one request; an unsupported strategy raises BusinessRuleViolation (422)."""
        return _to_dto(self._service.quote(request))

    def quote_many(self, requests: Sequence[PricingRequest]) -> dict[UUID, QuoteDTO]:
        """One quote per request, keyed by room type id."""
        return {room_type_id: _to_dto(quote) for room_type_id, quote in self._service.quote_many(requests).items()}


def get_pricing_api() -> PricingAPI:
    return PricingAPI(pricing_service())


def _to_dto(quote: PriceQuote) -> QuoteDTO:
    return QuoteDTO(
        room_type_id=quote.room_type_id,
        strategy=quote.strategy,
        rooms=quote.rooms,
        nightly_rates=tuple((rate.night, rate.amount) for rate in quote.nightly_rates),
        total=quote.total,
        average_nightly=quote.average_nightly,
    )
