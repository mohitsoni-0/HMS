"""Read models the inventory hands to other contexts (LLD §5.5). Immutable and Django-free."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from inventory.public.enums import HoldStatus


@dataclass(frozen=True, slots=True)
class HoldView:
    hold_id: UUID
    status: HoldStatus
    expires_at: datetime | None
