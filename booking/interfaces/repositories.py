"""Repository port of the booking aggregate (LLD §8.3)."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from booking.domain.booking import Booking


class BookingRepository(ABC):
    @abstractmethod
    def add(self, booking: Booking) -> None: ...

    @abstractmethod
    def get(self, booking_id: UUID) -> Booking:
        """The booking, or NotFound(BOOKING_NOT_FOUND)."""

    @abstractmethod
    def get_for_update(self, booking_id: UUID) -> Booking:
        """Like ``get``, locking the row until the transaction ends (SELECT ... FOR UPDATE)."""

    @abstractmethod
    def save(self, booking: Booking) -> None: ...

    @abstractmethod
    def list_for_guest(
        self, guest_id: str, *, page: int, page_size: int, property_id: UUID | None = None
    ) -> tuple[list[Booking], int]:
        """A page of the guest's bookings, newest first, optionally narrowed to one property, and the total."""

    @abstractmethod
    def list_for_property(self, property_id: UUID, *, page: int, page_size: int) -> tuple[list[Booking], int]:
        """A page of the property's bookings, newest first, and the total."""

    @abstractmethod
    def find_expired_pending_ids(self, now: datetime, limit: int) -> list[UUID]:
        """Ids of PENDING_PAYMENT bookings with ``hold_expires_at <= now``, oldest expiry first. No locks."""

    @abstractmethod
    def lock_if_expired_pending(self, booking_id: UUID, now: datetime) -> Booking | None:
        """The booking locked FOR UPDATE SKIP LOCKED if it still matches the expiry predicate; else None
        (gone, already handled, or being handled by another worker)."""
