"""Django implementation of ``BookingRepository`` (LLD §2.4, §8.3).

``add`` inserts inside its own savepoint, so a duplicate (guest, Idempotency-Key) becomes
``DuplicateIdempotencyKey`` while the enclosing transaction stays usable; ``save`` loads the row inside the
caller's transaction (already locked by ``get_for_update`` or ``lock_if_expired_pending``), copies the
mutable fields and saves only those, so ``AddCommonField.save`` stamps the audit columns and appends the log
row. That unique key is the only one besides the primary key; the CHECKs repeat domain invariants, so any
other constraint violation is a bug: it propagates (500) instead of being translated into a client error
(LLD §2.5).
"""

import logging
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import QuerySet

from booking.domain.booking import Booking
from booking.enums import BookingStatus
from booking.interfaces.repositories import BookingRepository, DuplicateIdempotencyKey
from booking.models.booking_models import BOOKING_IDEMPOTENCY_KEY_CONSTRAINT
from booking.models.booking_models import Booking as BookingModel
from booking.repositories import mappers
from booking.status_code import BOOKING_NOT_FOUND

logger = logging.getLogger(__name__)

_NEWEST_FIRST = ("-creation_date", "-id")


class DjangoBookingRepository(BookingRepository):
    def add(self, booking: Booking) -> None:
        row = mappers.new_row(booking)
        try:
            with transaction.atomic():
                row.save(force_insert=True)
        except IntegrityError as exc:
            if _violates(exc, BOOKING_IDEMPOTENCY_KEY_CONSTRAINT):
                logger.info("Booking %s rejected: the guest already used its idempotency key", booking.id)
                raise DuplicateIdempotencyKey(booking.id) from exc
            raise
        booking.created_at = row.creation_date  # stamped by the insert (auto_now_add)

    def get(self, booking_id: UUID) -> Booking:
        return mappers.to_domain(BookingModel.objects_active.get_or_not_found(BOOKING_NOT_FOUND, pk=booking_id))

    def get_for_update(self, booking_id: UUID) -> Booking:
        row = BookingModel.objects_active.select_for_update().get_or_not_found(BOOKING_NOT_FOUND, pk=booking_id)
        return mappers.to_domain(row)

    def find_by_idempotency_key(self, guest_id: str, idempotency_key: str) -> Booking | None:
        row = BookingModel.objects_active.filter(guest_id=guest_id, idempotency_key=idempotency_key).first()
        return None if row is None else mappers.to_domain(row)

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

    def find_expired_pending_ids(
        self, now: datetime, limit: int, *, exclude: Collection[UUID] = frozenset()
    ) -> list[UUID]:
        candidates = _expired_pending(now).exclude(pk__in=list(exclude)) if exclude else _expired_pending(now)
        return list(candidates.order_by("hold_expires_at", "id").values_list("id", flat=True)[:limit])

    def lock_if_expired_pending(self, booking_id: UUID, now: datetime) -> Booking | None:
        row = _expired_pending(now).select_for_update(skip_locked=True).filter(pk=booking_id).first()
        return None if row is None else mappers.to_domain(row)


def _expired_pending(now: datetime) -> QuerySet:
    """The expiry predicate, as in ``Booking.hold_expired``: pending and ``now >= hold_expires_at``."""
    return BookingModel.objects_active.filter(booking_status=BookingStatus.PENDING_PAYMENT, hold_expires_at__lte=now)


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """True when ``exc`` was raised by ``constraint``; psycopg reports the name in ``diag.constraint_name``."""
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint


def _page(bookings: QuerySet, page: int, page_size: int) -> tuple[list[Booking], int]:
    start = (page - 1) * page_size
    rows = bookings.order_by(*_NEWEST_FIRST)[start : start + page_size]
    return [mappers.to_domain(row) for row in rows], bookings.count()
