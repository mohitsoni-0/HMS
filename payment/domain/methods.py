"""Payment method strategies (LLD §9.2, HLD §3.4): one handler per method, chosen through a registry.

A handler validates the raw details of its method and returns the masked instrument plus the transient
gateway payload. Errors name the offending field, never its value:
``ValidationFailed(INVALID_PAYMENT_DETAILS, reason="card_number")`` renders "Invalid card_number". Adding a
method is a new handler, enum member and details serializer in ``payment.public.serializers``; the charge
service and the callers do not change.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.validation import is_indian_mobile
from hotel_commons.exceptions import ValidationFailed

from payment.domain.instrument import ParsedInstrument, PaymentInstrument
from payment.enums import CardBrand, PaymentMethod, WalletProvider
from payment.status_code import INVALID_PAYMENT_DETAILS, UNSUPPORTED_PAYMENT_METHOD

INSTRUMENT_DISPLAY_MAX_LENGTH: Final = 64  # payment.instrument_display is varchar(64)
MAX_EXPIRY_YEARS_AHEAD: Final = 20

_CARD_NUMBER: Final = re.compile(r"[0-9]{12,19}")
_CARD_NUMBER_SEPARATORS: Final = re.compile(r"[ -]")
_DIGITS: Final = re.compile(r"[0-9]+")
_VPA: Final = re.compile(r"[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}")
_HOLDER_NAME_LENGTH: Final = range(2, 101)
_VISIBLE_DIGITS: Final = 4
_CARD_MASK: Final = "••••"
_PHONE_MASK: Final = "******"
_VPA_MASK: Final = "****"


class PaymentMethodHandler(ABC):
    method: ClassVar[PaymentMethod]

    @abstractmethod
    def parse(self, details: Mapping[str, Any]) -> ParsedInstrument:
        """Validate ``details``; ValidationFailed(INVALID_PAYMENT_DETAILS, reason=<field name>) when invalid."""


@dataclass(frozen=True, slots=True)
class _BrandPrefix:
    """Card numbers whose leading digits, read as a number as wide as ``low``, fall in ``low..high``."""

    brand: CardBrand
    low: int
    high: int

    def matches(self, number: str) -> bool:
        return self.low <= int(number[: len(str(self.low))]) <= self.high


_BRAND_PREFIXES: Final = (
    _BrandPrefix(CardBrand.VISA, 4, 4),
    _BrandPrefix(CardBrand.MASTERCARD, 51, 55),
    _BrandPrefix(CardBrand.MASTERCARD, 2221, 2720),
    _BrandPrefix(CardBrand.RUPAY, 60, 60),
    _BrandPrefix(CardBrand.RUPAY, 65, 65),
    _BrandPrefix(CardBrand.RUPAY, 81, 82),
    _BrandPrefix(CardBrand.AMEX, 34, 34),
    _BrandPrefix(CardBrand.AMEX, 37, 37),
)


class CardHandler(PaymentMethodHandler):
    """Card number (12-19 digits, Luhn, known brand), expiry not past, CVV (4 digits for AMEX, else 3), holder.

    Only the brand and the last four digits survive in the instrument; the rest goes to the gateway only.
    """

    method = PaymentMethod.CARD

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def parse(self, details: Mapping[str, Any]) -> ParsedInstrument:
        number = _CARD_NUMBER_SEPARATORS.sub("", _text(details, "card_number"))
        if not _CARD_NUMBER.fullmatch(number) or not _passes_luhn(number):
            raise _invalid("card_number")
        brand = _card_brand(number)
        month, year = self._expiry(details)
        cvv = _text(details, "cvv")
        if not _DIGITS.fullmatch(cvv) or len(cvv) != (4 if brand is CardBrand.AMEX else 3):
            raise _invalid("cvv")
        holder_name = _text(details, "holder_name").strip()
        if len(holder_name) not in _HOLDER_NAME_LENGTH:
            raise _invalid("holder_name")
        instrument = PaymentInstrument(self.method, f"{brand.name} {_CARD_MASK} {number[-_VISIBLE_DIGITS:]}")
        payload = {
            "card_number": number,
            "expiry_month": f"{month:02d}",
            "expiry_year": str(year),
            "cvv": cvv,
            "holder_name": holder_name,
        }
        return ParsedInstrument(instrument, payload)

    def _expiry(self, details: Mapping[str, Any]) -> tuple[int, int]:
        """(month, year); a card is valid until the end of its expiry month."""
        today = self._clock.today()
        month = _whole_number(details, "expiry_month")
        if not 1 <= month <= 12:
            raise _invalid("expiry_month")
        year = _whole_number(details, "expiry_year")
        if not today.year <= year <= today.year + MAX_EXPIRY_YEARS_AHEAD:
            raise _invalid("expiry_year")
        if year == today.year and month < today.month:
            raise _invalid("expiry_month")
        return month, year


class UpiHandler(PaymentMethodHandler):
    """UPI virtual payment address ``name@handle``; kept encrypted as the instrument's sensitive reference."""

    method = PaymentMethod.UPI

    def parse(self, details: Mapping[str, Any]) -> ParsedInstrument:
        vpa = _text(details, "vpa")
        if not _VPA.fullmatch(vpa):
            raise _invalid("vpa")
        local, _, handle = vpa.rpartition("@")
        display = f"{local[0]}{_VPA_MASK}@{handle}"[:INSTRUMENT_DISPLAY_MAX_LENGTH]
        return ParsedInstrument(PaymentInstrument(self.method, display, sensitive_ref=vpa), {"vpa": vpa})


class WalletHandler(PaymentMethodHandler):
    """Wallet provider (a ``WalletProvider`` member or its name) and the 10-digit mobile number of the wallet."""

    method = PaymentMethod.WALLET

    def parse(self, details: Mapping[str, Any]) -> ParsedInstrument:
        provider = _wallet_provider(details.get("provider"))
        phone = _text(details, "phone")
        if not is_indian_mobile(phone):
            raise _invalid("phone")
        display = f"{provider.name} {_PHONE_MASK}{phone[-_VISIBLE_DIGITS:]}"
        payload = {"provider": provider.name, "phone": phone}
        return ParsedInstrument(PaymentInstrument(self.method, display, sensitive_ref=phone), payload)


class PaymentMethodRegistry:
    """The handlers by method; the composition root registers one per ``PaymentMethod``."""

    def __init__(self) -> None:
        self._handlers: dict[PaymentMethod, PaymentMethodHandler] = {}

    def register(self, handler: PaymentMethodHandler) -> None:
        if handler.method in self._handlers:
            raise ValueError(f"A handler for {handler.method.name} is already registered")
        self._handlers[handler.method] = handler

    def get(self, method: PaymentMethod | str) -> PaymentMethodHandler:
        """The handler for a member or its name; ValidationFailed(UNSUPPORTED_PAYMENT_METHOD) otherwise."""
        member = method if isinstance(method, PaymentMethod) else _method_named(method)
        handler = self._handlers.get(member) if member is not None else None
        if handler is None:
            raise ValidationFailed(UNSUPPORTED_PAYMENT_METHOD, errors={"method": "is not supported"})
        return handler


def _invalid(field_name: str) -> ValidationFailed:
    return ValidationFailed(INVALID_PAYMENT_DETAILS, errors={field_name: "is invalid"}, reason=field_name)


def _text(details: Mapping[str, Any], field_name: str) -> str:
    value = details.get(field_name)
    if not isinstance(value, str):
        raise _invalid(field_name)
    return value


def _whole_number(details: Mapping[str, Any], field_name: str) -> int:
    value = details.get(field_name)
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field_name)
    return value


def _passes_luhn(number: str) -> bool:
    total = 0
    for position, digit in enumerate(int(character) for character in reversed(number)):
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _card_brand(number: str) -> CardBrand:
    for prefix in _BRAND_PREFIXES:
        if prefix.matches(number):
            return prefix.brand
    raise _invalid("card_number")


def _wallet_provider(value: object) -> WalletProvider:
    if isinstance(value, WalletProvider):
        return value
    provider = WalletProvider.__members__.get(value) if isinstance(value, str) else None
    if provider is None:
        raise _invalid("provider")
    return provider


def _method_named(name: object) -> PaymentMethod | None:
    return PaymentMethod.__members__.get(name) if isinstance(name, str) else None
