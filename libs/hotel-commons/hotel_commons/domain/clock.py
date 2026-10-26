"""Time port (LLD §3.4): services take a ``Clock`` instead of calling ``datetime.now()``, so tests can fix time.

Instants are timezone-aware UTC. Calendar dates (today, the first night of a stay, a check-in instant) are
in the business time zone, Asia/Kolkata, where the properties are.
"""

from datetime import UTC, date, datetime
from typing import Final, Protocol
from zoneinfo import ZoneInfo

BUSINESS_TIME_ZONE: Final = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware UTC."""
        ...

    def today(self) -> date:
        """Today's date in the business time zone."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return business_date(self.now())


def business_date(instant: datetime) -> date:
    """The calendar date of an aware instant in Asia/Kolkata: 2026-01-01T18:30Z falls on 2026-01-02."""
    if instant.utcoffset() is None:
        raise ValueError("business_date needs a timezone-aware datetime")
    return instant.astimezone(BUSINESS_TIME_ZONE).date()
