"""Inventory holds under real concurrency (LLD §5.4, §14 "Concurrency (db)").

Each racing thread runs ``InventoryAPI.hold`` on its own database connection, so these tests commit
(``transaction=True``) and clean up after themselves: Django's flush between transactional tests only truncates
tables on the search path, and the context tables live in their own schemas (LLD §3.15).
"""

import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from django.db import connection
from django.utils import timezone
from hotel_commons.domain.clock import SystemClock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict
from uuid6 import uuid7

from inventory.models.inventory_models import Allotment, InventoryHold, InventoryHoldLog, RoomNight
from inventory.public.api import get_inventory_api
from inventory.status_code import INVENTORY_UNAVAILABLE

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

THREADS = 10
ROUNDS = 6
JOIN_TIMEOUT_SECONDS = 30


@pytest.fixture
def last_room() -> Iterator[UUID]:
    """A room type with a single room, provisioned over the whole horizon; its rows are deleted afterwards."""
    room_type_id = uuid7()
    get_inventory_api().provision(room_type_id, total_rooms=1)
    yield room_type_id
    InventoryHoldLog.objects.filter(room_type_id=room_type_id).delete()
    InventoryHold.objects.filter(room_type_id=room_type_id).delete()
    RoomNight.objects.filter(room_type_id=room_type_id).delete()
    Allotment.objects.filter(room_type_id=room_type_id).delete()


def _days_ahead(days: int) -> Any:
    return SystemClock().today() + timedelta(days=days)


def _race(attempts: list[Callable[[], Any]]) -> list[tuple[str, Any]]:
    """Run the attempts on their own threads, released together; one ("ok" | "conflict" | "error", value) each."""
    barrier = threading.Barrier(len(attempts))
    results: list[tuple[str, Any]] = [("error", "did not finish")] * len(attempts)

    def run(index: int, attempt: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=JOIN_TIMEOUT_SECONDS)
            results[index] = ("ok", attempt())
        except Conflict as exc:
            results[index] = ("conflict", exc.status_code)
        except Exception as exc:  # noqa: BLE001 - a deadlock or lock timeout must fail the assertion, not the thread
            results[index] = ("error", repr(exc))
        finally:
            connection.close()  # each thread opened its own connection

    threads = [threading.Thread(target=run, args=(index, attempt)) for index, attempt in enumerate(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT_SECONDS)
    return results


def _hold_attempt(room_type_id: UUID, stay: DateRange, reference: str) -> Callable[[], UUID]:
    def attempt() -> UUID:
        expires_at = timezone.now() + timedelta(minutes=15)
        hold = get_inventory_api().hold(
            room_type_id=room_type_id, stay=stay, rooms=1, reference=reference, expires_at=expires_at
        )
        return hold.hold_id

    return attempt


def _reserved(room_type_id: UUID, stay: DateRange) -> dict[Any, int]:
    rows = RoomNight.objects.filter(room_type_id=room_type_id, stay_date__gte=stay.start, stay_date__lt=stay.end)
    return dict(rows.values_list("stay_date", "reserved_rooms"))


def test_last_room_race_single_winner(last_room: UUID) -> None:
    stay = DateRange(_days_ahead(5), _days_ahead(7))

    results = _race([_hold_attempt(last_room, stay, f"race-{n}") for n in range(THREADS)])

    outcomes = [kind for kind, _ in results]
    assert outcomes.count("ok") == 1, results
    assert results.count(("conflict", INVENTORY_UNAVAILABLE)) == THREADS - 1, results
    assert _reserved(last_room, stay) == {night: 1 for night in stay.dates()}
    assert InventoryHold.objects.filter(room_type_id=last_room).count() == 1


def test_overlapping_stays_no_deadlock(last_room: UUID) -> None:
    # Nights 1-3 and 2-4 share night 2, so with one room exactly one of each pair can win.
    first = DateRange(_days_ahead(11), _days_ahead(13))
    second = DateRange(_days_ahead(12), _days_ahead(14))
    nights = DateRange(first.start, second.end)

    for round_ in range(ROUNDS):
        stays = (first, second) if round_ % 2 == 0 else (second, first)

        results = _race([_hold_attempt(last_room, stay, f"overlap-{round_}-{n}") for n, stay in enumerate(stays)])

        assert sorted(kind for kind, _ in results) == ["conflict", "ok"], results  # no deadlock, no lock timeout
        assert ("conflict", INVENTORY_UNAVAILABLE) in results
        [(winner_stay, (_, hold_id))] = [(stay, result) for stay, result in zip(stays, results) if result[0] == "ok"]
        assert _reserved(last_room, nights) == {
            night: int(night in winner_stay.dates()) for night in nights.dates()
        }, f"round {round_}"
        get_inventory_api().release_hold(hold_id, "CANCELLED")
        assert set(_reserved(last_room, nights).values()) == {0}
