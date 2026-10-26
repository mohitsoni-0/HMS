"""Inventory tables in schema ``inventory`` (LLD §5.6, §3.15).

- ``Allotment`` and ``RoomNight`` keep no log: a night's counter is high-churn and derivable from the holds.
- ``InventoryHold`` follows the model triad (``CommonInventoryHold`` / ``InventoryHold`` / ``InventoryHoldLog``),
  so every state change of a hold is snapshotted with its actor and request id.

The CHECK constraints repeat the domain invariants, so the database refuses an oversold night even if
application code is wrong.
"""

from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.db.models import F, Q
from uuid6 import uuid7

from hotel_commons.db.schema import schema_table
from hotel_commons.models.base import AddCommonField
from hotel_commons.models.fields import EnumField
from hotel_commons.models.managers import CustomUpdateManager

from inventory.constants import MAX_REFERENCE_LENGTH
from inventory.enums import HoldStatus, ReleaseReason


class Allotment(AddCommonField):
    room_type_id = models.UUIDField(primary_key=True)
    total_rooms = models.IntegerField()

    class Meta:
        db_table = schema_table("inventory", "allotment")
        constraints = [
            models.CheckConstraint(condition=Q(total_rooms__gte=0), name="inventory_allotment_total_rooms_gte_0"),
        ]


class RoomNight(AddCommonField):
    """One row per room type per night; ``fillfactor = 80`` (set in the migration) keeps counter updates HOT."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    room_type_id = models.UUIDField()
    stay_date = models.DateField()
    total_rooms = models.IntegerField()
    reserved_rooms = models.IntegerField(default=0)

    class Meta:
        db_table = schema_table("inventory", "room_night")
        constraints = [
            models.UniqueConstraint(
                fields=["room_type_id", "stay_date"], name="inventory_room_night_room_type_date_uniq"
            ),
            models.CheckConstraint(
                condition=Q(reserved_rooms__gte=0) & Q(reserved_rooms__lte=F("total_rooms")),
                name="inventory_room_night_reserved_in_range",
            ),
            models.CheckConstraint(condition=Q(total_rooms__gte=0), name="inventory_room_night_total_rooms_gte_0"),
        ]


class CommonInventoryHold(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    room_type_id = models.UUIDField()
    check_in = models.DateField()
    check_out = models.DateField()
    rooms = models.SmallIntegerField()
    reference = models.CharField(max_length=MAX_REFERENCE_LENGTH)
    hold_status = EnumField(HoldStatus)
    expires_at = models.DateTimeField(null=True, blank=True)
    release_reason = EnumField(ReleaseReason, null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class InventoryHold(CommonInventoryHold):
    CustomUpdateManager.set_logModel(logModel="inventory.InventoryHoldLog", model="inventory.InventoryHold")

    class Meta:
        db_table = schema_table("inventory", "hold")
        indexes = [models.Index(fields=["reference"], name="inventory_hold_reference_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(check_out__gt=F("check_in")), name="inventory_hold_dates_valid"),
            models.CheckConstraint(condition=Q(rooms__gte=1), name="inventory_hold_rooms_gte_1"),
        ]


class InventoryHoldLog(CommonInventoryHold):
    log = models.ForeignKey(InventoryHold, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table("inventory", "hold_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="inventory_hold_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="inventory_hold_log_brin"),
        ]
