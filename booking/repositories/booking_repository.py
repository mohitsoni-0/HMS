"""Django implementation of ``BookingRepository`` (LLD §2.4, §8.3).

``add`` inserts; ``save`` loads the row inside the caller's transaction (already locked by
``get_for_update`` or ``lock_if_expired_pending``), copies the mutable fields and saves only those, so
``AddCommonField.save`` stamps the audit columns and appends the log row. The table has no unique keys
besides the primary key and its CHECKs repeat domain invariants, so a constraint violation here is a bug:
it propagates (500) instead of being translated into a client error (LLD §2.5).
"""

from datetime import datetime
from uuid import UUID

from django.db.models import QuerySet

from booking.domain.booking import Booking
from booking.enums import BookingStatus
from booking.interfaces.repositories import BookingRepository
from booking.models.booking_models import Booking as BookingModel
from booking.repositories import mappers
from booking.status_code import BOOKING_NOT_FOUND

_NEWEST_FIRST = ("-creation_date", "-id")


class DjangoBookingRepository(BookingRepository):
    def add(self, booking: Booking) -> None:
        mappers.new_row(booking).save(force_insert=True)

    def get(self, booking_id: UUID) -> Booking:
        return mappers.to_domain(BookingModel.objects_active.get_or_not_found(BOOKING_NOT_FOUND, pk=booking_id))

    def get_for_update(self, booking_id: UUID) -> Booking:
        row = BookingModel.objects_active.select_for_update().get_or_not_found(BOOKING_NOT_FOUND, pk=booking_id)
        return mappers.to_domain(row)

    def save(self, booking: Booking) -> None:
        row = BookingModel.objects.get(pk=booking.id)
        row.save(update_fields=mappers.apply(booking, row))

    def list_for_guest(
        self, guest_id: str, *, page: int, page_size: int, property_id: UUID | None = None
    ) -> tuple[list[Booking], int]:
        bookings = BookingModel.objects_active.filter(guest_id=guest_id)
        if property_id is not None:
            bookings = bookings.filter(property_id=property_id)
        return _page(bookings, page, page_size)

    def list_for_property(self, property_id: UUID, *, page: int, page_size: int) -> tuple[list[Booking], int]:
        return _page(BookingModel.objects_active.filter(property_id=property_id), page, page_size)

    def find_expired_pending_ids(self, now: datetime, limit: int) -> list[UUID]:
        return list(_expired_pending(now).order_by("hold_expires_at").values_list("id", flat=True)[:limit])

    def lock_if_expired_pending(self, booking_id: UUID, now: datetime) -> Booking | None:
        row = _expired_pending(now).select_for_update(skip_locked=True).filter(pk=booking_id).first()
        return None if row is None else mappers.to_domain(row)


def _expired_pending(now: datetime) -> QuerySet:
    """The expiry predicate, as in ``Booking.hold_expired``: pending and ``now >= hold_expires_at``."""
    return BookingModel.objects_active.filter(booking_status=BookingStatus.PENDING_PAYMENT, hold_expires_at__lte=now)


def _page(bookings: QuerySet, page: int, page_size: int) -> tuple[list[Booking], int]:
    start = (page - 1) * page_size
    rows = bookings.order_by(*_NEWEST_FIRST)[start : start + page_size]
    return [mappers.to_domain(row) for row in rows], bookings.count()
