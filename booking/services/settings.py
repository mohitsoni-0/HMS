"""Configurable booking rules (LLD §8.2). ``booking.container`` builds them from Django settings."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class BookingSettings:
    hold_ttl: timedelta  # BOOKING_HOLD_TTL_SECONDS: how long a prepaid booking holds rooms unpaid
    max_nights: int  # MAX_STAY_NIGHTS
    horizon_days: int  # INVENTORY_HORIZON_DAYS: how far ahead a stay may end
