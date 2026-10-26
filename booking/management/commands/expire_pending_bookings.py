"""``python manage.py expire_pending_bookings [--batch-size N] [--time-budget S]``: expire unpaid holds
(LLD §8.4, §13).

The scheduler runs it every minute. One run works through the backlog batch by batch until a batch comes
back short or the time budget (45 s by default, inside the 60 s interval) is spent. Each booking is handled
in its own transaction, so the command can run on several workers at once (SKIP LOCKED) and a failure on
one booking never blocks the rest.
"""

from argparse import ArgumentParser
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand

from booking import container
from booking.constants import EXPIRE_BATCH_SIZE, EXPIRE_TIME_BUDGET_SECONDS


class Command(BaseCommand):
    help = "Expire PENDING_PAYMENT bookings whose hold has lapsed and release their rooms."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--batch-size", type=int, default=EXPIRE_BATCH_SIZE, help="Bookings per batch.")
        parser.add_argument(
            "--time-budget",
            type=float,
            default=EXPIRE_TIME_BUDGET_SECONDS,
            help="Seconds after which no further batch is started.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        expired = container.expire_bookings_service().execute(
            batch_size=options["batch_size"], time_budget=timedelta(seconds=options["time_budget"])
        )
        self.stdout.write(f"Expired {expired} booking(s)")
