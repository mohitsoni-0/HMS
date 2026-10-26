"""Money as integer paisa, the minor unit (₹1 = 100 paisa), end to end (LLD §2.10, §3.4).

No floats anywhere: rupees enter only through ``Money.of_rupees`` (exact Decimal parsing) and every rounding
is ROUND_HALF_UP to a whole paisa, computed in a private decimal context so a caller's global context can
never change a result. An amount is never negative and always fits a PostgreSQL BIGINT column. Only INR is
supported (assumption A4); the currency checks keep mixed-currency arithmetic impossible if that changes.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, DecimalException, DivisionByZero, InvalidOperation, Overflow
from typing import Final, Self

from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED

DEFAULT_CURRENCY: Final = "INR"
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({DEFAULT_CURRENCY})
MAX_PAISA: Final = 2**63 - 1  # PostgreSQL BIGINT

_CONTEXT: Final = Context(prec=40, rounding=ROUND_HALF_UP, traps=[InvalidOperation, DivisionByZero, Overflow])
_MAX_RUPEES: Final = Decimal(MAX_PAISA).scaleb(-2, context=_CONTEXT)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class Money:
    """A non-negative amount in paisa. Immutable; equal and hashed by (paisa, currency)."""

    paisa: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if type(self.paisa) is not int:  # bool, float and Decimal are refused: convert with of_rupees()
            raise _invalid("amount", "must be a whole number of paisa")
        if self.paisa < 0:
            raise _invalid("amount", "must not be negative")
        if self.paisa > MAX_PAISA:
            raise _invalid("amount", "is too large")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise _invalid("currency", f"must be {DEFAULT_CURRENCY}")

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(0, currency)

    @classmethod
    def of_rupees(cls, rupees: Decimal | str | int) -> Self:
        """Exact rupees to paisa: ``Money.of_rupees("4800.50").paisa == 480050``.

        Floats, NaN or Infinity, negatives, sub-paisa precision ("1.005") and amounts beyond BIGINT raise
        ValidationFailed. Trailing zeros are fine: "4800.500" is ₹4800.50.
        """
        amount = _parse_rupees(rupees)
        return cls(int(amount.scaleb(2, context=_CONTEXT)))

    @property
    def rupees(self) -> Decimal:
        """The amount in rupees with exactly two places: ``Money(480050).rupees == Decimal("4800.50")``."""
        return Decimal(self.paisa).scaleb(-2, context=_CONTEXT)

    def __add__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paisa + self._same_currency(other).paisa, self.currency)

    def __sub__(self, other: object) -> "Money":
        """The difference; a negative result raises ValidationFailed."""
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paisa - self._same_currency(other).paisa, self.currency)

    def times(self, factor: int | Decimal) -> "Money":
        """paisa × factor, ROUND_HALF_UP: ``Money(1000).times(Decimal("1.20")) == Money(1200)``."""
        return Money(_scale(self.paisa, _factor(factor, "factor")), self.currency)

    def percent(self, pct: int | Decimal) -> "Money":
        """``pct`` per cent, 0..100, ROUND_HALF_UP: ``Money(999).percent(Decimal(50)) == Money(500)``."""
        share = _factor(pct, "pct")
        if share > _HUNDRED:
            raise ValueError("pct must be between 0 and 100")
        return Money(_scale(self.paisa, share, _HUNDRED), self.currency)

    def divide(self, n: int) -> "Money":
        """One of ``n`` equal parts, ROUND_HALF_UP (per-night averages), in exact integer arithmetic."""
        if type(n) is not int:
            raise TypeError(f"n must be an int, not {type(n).__name__}")
        if n < 1:
            raise ValueError("n must be at least 1")
        quotient, remainder = divmod(self.paisa, n)
        return Money(quotient + 1 if 2 * remainder >= n else quotient, self.currency)

    def is_positive(self) -> bool:
        return self.paisa > 0

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.paisa < self._same_currency(other).paisa

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.paisa <= self._same_currency(other).paisa

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.paisa > self._same_currency(other).paisa

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.paisa >= self._same_currency(other).paisa

    def _same_currency(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise _invalid("currency", f"cannot combine {self.currency} with {other.currency}")
        return other


def _invalid(field: str, reason: str) -> ValidationFailed:
    return ValidationFailed(VALIDATION_FAILED, errors={field: reason})


def _parse_rupees(rupees: object) -> Decimal:
    if isinstance(rupees, bool) or not isinstance(rupees, Decimal | str | int):
        raise _invalid("amount", "must be a decimal string, Decimal or int, never a float")
    try:
        amount = Decimal(rupees)
    except InvalidOperation:
        raise _invalid("amount", "is not a number") from None
    if not amount.is_finite():
        raise _invalid("amount", "must be a finite number")
    if amount < 0:
        raise _invalid("amount", "must not be negative")
    if amount > _MAX_RUPEES:
        raise _invalid("amount", "is too large")
    if _has_sub_paisa(amount):
        raise _invalid("amount", "must have at most 2 decimal places")
    return amount


def _has_sub_paisa(amount: Decimal) -> bool:
    """True when a digit beyond the second decimal place is non-zero; exact, no rounding involved."""
    _, digits, exponent = amount.as_tuple()
    extra = -2 - int(exponent)
    return extra > 0 and any(digits[-extra:])


def _factor(value: object, name: str) -> Decimal:
    if type(value) is int:
        value = Decimal(value)
    elif not isinstance(value, Decimal):
        raise TypeError(f"{name} must be an int or Decimal, not {type(value).__name__}")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return value


def _scale(paisa: int, factor: Decimal, divisor: Decimal = _ONE) -> int:
    """paisa × factor / divisor, rounded half up to a whole paisa in the private context."""
    try:
        exact = _CONTEXT.divide(_CONTEXT.multiply(Decimal(paisa), factor), divisor)
        return int(exact.quantize(_ONE, rounding=ROUND_HALF_UP, context=_CONTEXT))
    except DecimalException:  # more digits than the context holds: far beyond BIGINT anyway
        raise _invalid("amount", "is too large") from None
