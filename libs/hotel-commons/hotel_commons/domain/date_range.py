"""Stay dates as a half-open range ``[start, end)`` (LLD §3.4, assumption A2).

``end`` is the check-out day: it is not a night and is not charged, so back-to-back stays never overlap.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if not (_is_calendar_date(self.start) and _is_calendar_date(self.end)):
            raise TypeError("DateRange needs datetime.date values, not datetimes")
        if self.start >= self.end:
            raise ValidationFailed(VALIDATION_FAILED, errors={"dates": "check_out must be after check_in"})

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    def dates(self) -> tuple[date, ...]:
        """Every night of the stay, in order: [2026-11-20, 2026-11-22) -> (20th, 21st)."""
        return tuple(self.start + timedelta(days=offset) for offset in range(self.nights))

    def overlaps(self, other: "DateRange") -> bool:
        return self.start < other.end and other.start < self.end


def _is_calendar_date(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)
