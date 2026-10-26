"""Inventory holds under real concurrency (LLD §5.4, §14 "Concurrency (db)").

Each racing thread runs ``InventoryAPI.hold`` on its own database connection, so these tests commit
(``transaction=True``) and clean up after themselves: Django's flush between transactional tests only truncates
tables on the search path, and the context tables live in their own schemas (LLD §3.15).
"""

import re
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from hotel_commons.domain.clock import SystemClock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict
from uuid6 import uuid7

from inventory.models.inventory_models import Allotment, InventoryHold, InventoryHoldLog, RoomNight
from inventory.public.api import get_inventory_api
from inventory.repositories.mappers import room_night_to_domain
from inventory.repositories.room_night_repository import DjangoRoomNightRepository
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


# Overlapping stays race on the nights they share. A lock-order deadlock needs at least two shared nights
# locked in opposite directions, so these stays share three (nights 12-14 of 11-14 and 12-15).
FIRST_STAY = (11, 15)
SECOND_STAY = (12, 16)
OVERLAP_ROUNDS = 20


def _overlapping_stays() -> tuple[DateRange, DateRange]:
    return DateRange(_days_ahead(FIRST_STAY[0]), _days_ahead(FIRST_STAY[1])), DateRange(
        _days_ahead(SECOND_STAY[0]), _days_ahead(SECOND_STAY[1])
    )


def test_overlapping_stays_no_deadlock(last_room: UUID) -> None:
    """Holds on stays sharing three nights, in both assignments, over many rounds: one wins, never an error.

    Regression caught: a deadlock or lock timeout between overlapping multi-night holds (an "error" outcome
    instead of ok + conflict), or an oversold or leaked night counter. The nights are locked by one
    ``SELECT ... ORDER BY stay_date FOR UPDATE``, so a real deadlock needs a lock-order regression *and*
    unlucky timing; ``test_lock_for_stay_locks_nights_in_date_order`` pins the order deterministically and
    ``test_opposite_lock_order_is_reported_as_an_error`` proves this harness turns a deadlock into a failure.
    """
    first, second = _overlapping_stays()
    nights = DateRange(first.start, second.end)
    assert len(set(first.dates()) & set(second.dates())) >= 2

    for round_ in range(OVERLAP_ROUNDS):
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


def test_hold_racing_a_release_of_an_overlapping_stay(last_room: UUID) -> None:
    """A release (which locks its nights too) racing a hold of an overlapping stay: no deadlock, no lost update."""
    first, second = _overlapping_stays()
    nights = DateRange(first.start, second.end)

    for round_ in range(OVERLAP_ROUNDS):
        held, wanted = (first, second) if round_ % 2 == 0 else (second, first)
        hold_id = _hold_attempt(last_room, held, f"release-{round_}-held")()

        release = lambda hold_id=hold_id: get_inventory_api().release_hold(hold_id, "CANCELLED")  # noqa: E731
        results = _race([release, _hold_attempt(last_room, wanted, f"release-{round_}-wanted")])

        assert results[0] == ("ok", None), results
        assert results[1][0] in {"ok", "conflict"}, results  # the hold may run before or after the release
        expected = {night: int(results[1][0] == "ok" and night in wanted.dates()) for night in nights.dates()}
        assert _reserved(last_room, nights) == expected, f"round {round_}: {results}"
        if results[1][0] == "ok":
            get_inventory_api().release_hold(results[1][1], "CANCELLED")


def test_lock_for_stay_locks_nights_in_date_order(last_room: UUID) -> None:
    """The deterministic half of the guarantee: one FOR UPDATE statement, ordered by stay_date ascending."""
    first, _ = _overlapping_stays()

    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        locked = DjangoRoomNightRepository().lock_for_stay(last_room, first)

    assert [night.stay_date for night in locked] == list(first.dates())
    [sql] = [query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"]]
    assert re.search(r'ORDER BY "?[\w"\.]*"?stay_date"? ASC FOR UPDATE$', sql), sql


def test_opposite_lock_order_is_reported_as_an_error(last_room: UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: lock the shared nights in opposite directions and the race reports a deadlock.

    The patched repository locks night by night and waits on a barrier once each side holds one shared night,
    which makes the lock cycle certain. PostgreSQL then aborts one side with "deadlock detected", and the
    harness reports it as an "error" outcome: exactly what ``test_overlapping_stays_no_deadlock`` rejects.
    """
    first, second = _overlapping_stays()
    both_hold_a_shared_night = threading.Barrier(2)

    def lock_night_by_night(self: DjangoRoomNightRepository, room_type_id: UUID, stay: DateRange) -> list[Any]:
        ascending = stay == first
        dates = list(stay.dates()) if ascending else list(reversed(stay.dates()))
        shared = set(first.dates()) & set(second.dates())
        rows = []
        for night in dates:
            rows.append(RoomNight.objects.select_for_update().get(room_type_id=room_type_id, stay_date=night))
            if night in shared and len([row for row in rows if row.stay_date in shared]) == 1:
                both_hold_a_shared_night.wait(timeout=JOIN_TIMEOUT_SECONDS)
        return [room_night_to_domain(row) for row in sorted(rows, key=lambda row: row.stay_date)]

    monkeypatch.setattr(DjangoRoomNightRepository, "lock_for_stay", lock_night_by_night)

    results = _race([_hold_attempt(last_room, first, "cycle-0"), _hold_attempt(last_room, second, "cycle-1")])

    errors = [value for kind, value in results if kind == "error"]
    assert len(errors) == 1 and "deadlock detected" in errors[0], results
    assert [kind for kind, _ in results].count("ok") == 1, results
