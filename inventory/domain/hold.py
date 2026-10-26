"""A hold on ``rooms`` rooms of one room type for every night of a stay (LLD §5.2).

HELD while the booking awaits payment, CONFIRMED once it is paid (or pay-at-property), RELEASED when it is
cancelled or expires. Placing a hold and releasing it move the per-night counters; confirming does not.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from uuid6 import uuid7

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict, ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED

from inventory.constants import MAX_REFERENCE_LENGTH
from inventory.enums import HoldStatus, ReleaseReason
from inventory.status_code import INVALID_HOLD_TRANSITION


@dataclass(slots=True)
class InventoryHold:
    id: UUID
    room_type_id: UUID
    stay: DateRange
    rooms: int
    reference: str
    status: HoldStatus
    expires_at: datetime | None
    release_reason: ReleaseReason | None = None
    released_at: datetime | None = None

    @classmethod
    def place(
        cls, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> Self:
        """A new HELD hold; ``expires_at`` is None for a hold that only a release ends (pay at property)."""
        _require_rooms(rooms)
        _require_reference(reference)
        if expires_at is not None and expires_at.utcoffset() is None:
            raise ValidationFailed(VALIDATION_FAILED, errors={"expires_at": "must be timezone-aware"})
        return cls(
            id=uuid7(),
            room_type_id=room_type_id,
            stay=stay,
            rooms=rooms,
            reference=reference,
            status=HoldStatus.HELD,
            expires_at=expires_at,
        )

    def confirm(self) -> None:
        """HELD -> CONFIRMED; confirming again is a no-op; a released hold cannot come back."""
        if self.status is HoldStatus.RELEASED:
            raise Conflict(INVALID_HOLD_TRANSITION, state=self.status.name)
        self.status = HoldStatus.CONFIRMED

    def release(self, reason: ReleaseReason, now: datetime) -> bool:
        """HELD or CONFIRMED -> RELEASED and True, so the caller returns the rooms; already released -> False."""
        if self.status is HoldStatus.RELEASED:
            return False
        self.status = HoldStatus.RELEASED
        self.release_reason = reason
        self.released_at = now
        return True


def _require_rooms(rooms: int) -> None:
    if type(rooms) is not int or rooms < 1:
        raise ValidationFailed(VALIDATION_FAILED, errors={"rooms": "must be a whole number, 1 or more"})


def _require_reference(reference: str) -> None:
    if not isinstance(reference, str) or not reference.strip() or len(reference) > MAX_REFERENCE_LENGTH:
        raise ValidationFailed(
            VALIDATION_FAILED, errors={"reference": f"must be 1-{MAX_REFERENCE_LENGTH} characters"}
        )
