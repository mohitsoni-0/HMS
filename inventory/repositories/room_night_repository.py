"""Django ``RoomNightRepository`` (LLD §5.3) over ``inventory.room_night``: the per-night counters.

Concurrency rests on two things: nights are locked in ``stay_date`` order (``lock_for_stay``), and the CHECK
``0 <= reserved_rooms <= total_rooms`` turns any oversell that slips past the application into an error.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Min, QuerySet

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict

from inventory.domain.room_night import RoomNight
from inventory.interfaces.repositories import RoomNightRepository
from inventory.models.inventory_models import RoomNight as RoomNightModel
from inventory.repositories.mappers import new_room_night_model, room_night_to_domain
from inventory.status_code import INVENTORY_UNAVAILABLE


class DjangoRoomNightRepository(RoomNightRepository):
    def ensure_rows(self, room_type_id: UUID, total_rooms: int, dates: Sequence[date]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING for the nights not stored yet.

        Reading the existing nights first keeps the daily horizon job and every hold from rewriting rows that
        are already there; ``ignore_conflicts`` still covers a night inserted concurrently.
        """
        existing = set(
            RoomNightModel.objects.filter(room_type_id=room_type_id, stay_date__in=dates).values_list(
                "stay_date", flat=True
            )
        )
        missing = [new_room_night_model(room_type_id, d, total_rooms) for d in sorted(set(dates) - existing)]
        if missing:
            RoomNightModel.objects.bulk_create(missing, ignore_conflicts=True)

    def lock_for_stay(self, room_type_id: UUID, stay: DateRange) -> list[RoomNight]:
        rows = _stay_nights(room_type_id, stay).select_for_update().order_by("stay_date")
        return [room_night_to_domain(row) for row in rows]

    def add_reserved(self, room_type_id: UUID, stay: DateRange, delta: int) -> None:
        """UPDATE ... SET reserved_rooms = reserved_rooms + delta, in its own savepoint.

        A violated CHECK, or a stay with a night missing, rolls back only that savepoint and raises
        Conflict(INVENTORY_UNAVAILABLE), so the caller's transaction stays usable.
        """
        try:
            with transaction.atomic():
                updated = _stay_nights(room_type_id, stay).update(reserved_rooms=F("reserved_rooms") + delta)
                if updated != stay.nights:
                    raise Conflict(INVENTORY_UNAVAILABLE)
        except IntegrityError:
            raise Conflict(INVENTORY_UNAVAILABLE) from None

    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange) -> dict[UUID, int]:
        """One aggregate over all candidates: MIN(total - reserved) per room type, HAVING COUNT(*) = nights."""
        rows = (
            RoomNightModel.objects.filter(
                room_type_id__in=room_type_ids, stay_date__gte=stay.start, stay_date__lt=stay.end
            )
            .order_by()
            .values("room_type_id")
            .annotate(min_avail=Min(F("total_rooms") - F("reserved_rooms")), nights=Count("id"))
            .filter(nights=stay.nights)
        )
        return {row["room_type_id"]: row["min_avail"] for row in rows}

    def occupancy_many(self, room_type_ids: Sequence[UUID], dates: Sequence[date]) -> dict[UUID, dict[date, Decimal]]:
        """One SELECT over every room type: only the counters, not whole rows."""
        if not room_type_ids or not dates:
            return {}
        rows = (
            RoomNightModel.objects.filter(room_type_id__in=room_type_ids, stay_date__in=dates)
            .order_by()
            .values_list("room_type_id", "stay_date", "total_rooms", "reserved_rooms")
        )
        occupancy: dict[UUID, dict[date, Decimal]] = {}
        for room_type_id, stay_date, total_rooms, reserved_rooms in rows:
            night = RoomNight(
                room_type_id=room_type_id, stay_date=stay_date, total_rooms=total_rooms, reserved_rooms=reserved_rooms
            )
            occupancy.setdefault(room_type_id, {})[stay_date] = night.occupancy
        return occupancy


def _stay_nights(room_type_id: UUID, stay: DateRange) -> QuerySet[RoomNightModel]:
    return RoomNightModel.objects.filter(room_type_id=room_type_id, stay_date__gte=stay.start, stay_date__lt=stay.end)
