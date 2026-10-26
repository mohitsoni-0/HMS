"""``python manage.py extend_inventory_horizon``: create the missing nights up to today + INVENTORY_HORIZON_DAYS.

Idempotent; the scheduler runs it every minute and it is safe to run by hand at any time (LLD §5.4, §13).
"""

from typing import Any

from django.core.management.base import BaseCommand

from inventory.public.api import get_inventory_api


class Command(BaseCommand):
    help = "Provision per-night inventory rows for every room type up to the booking horizon."

    def handle(self, *args: Any, **options: Any) -> None:
        room_types = get_inventory_api().extend_horizon()
        self.stdout.write(self.style.SUCCESS(f"Inventory horizon extended for {room_types} room type(s)"))
