"""A deterministic ``Clock`` for tests."""

from datetime import UTC, date, datetime, timedelta
from typing import Final

from hotel_commons.domain.clock import business_date

DEFAULT_NOW: Final = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)  # Monday, 12:00 in Asia/Kolkata


class FixedClock:
    """Frozen at an aware instant until ``advance`` moves it; satisfies ``hotel_commons.domain.clock.Clock``."""

    def __init__(self, now: datetime = DEFAULT_NOW) -> None:
        if now.utcoffset() is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = now.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return business_date(self._now)

    def advance(self, delta: timedelta) -> None:
        self._now += delta
