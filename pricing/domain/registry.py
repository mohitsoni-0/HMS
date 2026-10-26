"""Registry of pricing strategies by code (LLD §6); an unregistered code is a 422 business-rule error."""

from catalog.public.enums import PricingStrategyCode
from hotel_commons.exceptions import BusinessRuleViolation
from pricing.domain.strategies import DynamicPricing, PricingStrategy, StandardPricing, WeekendPricing
from pricing.status_code import UNSUPPORTED_PRICING_STRATEGY


class PricingStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[PricingStrategyCode, PricingStrategy] = {}

    def register(self, strategy: PricingStrategy) -> None:
        """Add ``strategy`` under its code; registering a second strategy for one code is a wiring error."""
        if strategy.code in self._strategies:
            raise ValueError(f"A pricing strategy is already registered for {strategy.code.name}")
        self._strategies[strategy.code] = strategy

    def get(self, code: PricingStrategyCode) -> PricingStrategy:
        try:
            return self._strategies[code]
        except KeyError:
            raise BusinessRuleViolation(UNSUPPORTED_PRICING_STRATEGY, strategy=code.name) from None

    def codes(self) -> frozenset[PricingStrategyCode]:
        return frozenset(self._strategies)


def build_default_registry() -> PricingStrategyRegistry:
    """A registry holding the STANDARD, WEEKEND and DYNAMIC strategies."""
    registry = PricingStrategyRegistry()
    for strategy in (StandardPricing(), WeekendPricing(), DynamicPricing()):
        registry.register(strategy)
    return registry
