"""Audit columns, log rows and PII at rest on the real models in PostgreSQL (LLD §3.5, §3.6, §14).

Ported from the phase-A model harness (SQLite, throwaway models) to the service's own tables: ``booking.booking``
and ``booking.booking_log``, ``catalog.owner_account`` and ``inventory.hold``/``hold_log``. Raw SQL reads what
is really stored, so the ciphertext is checked where it lives, not through the ORM that decrypts it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, time, timedelta
from typing import Any

import pytest
from django.core.exceptions import FieldError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from hotel_commons.context import RequestContext
from hotel_commons.domain.clock import SystemClock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.models import base as models_base
from hotel_commons.models.base import RecordStatus
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.pii import get_blind_indexer, get_pii_cipher
from hotel_commons.security.principal import Principal, Role
from uuid6 import uuid7

from booking.enums import BookingStatus, BookingType
from booking.models.booking_models import Booking as BookingRow
from booking.models.booking_models import BookingLog as BookingLogRow
from catalog.models.catalog_models import OwnerAccount as OwnerAccountRow
from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode
from inventory.enums import HoldStatus
from inventory.models.inventory_models import InventoryHoldLog
from inventory.public.api import get_inventory_api

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

GUEST = Principal(user_id="guest-7", roles=frozenset({Role.GUEST}))
ADMIN = Principal(user_id="admin-1", roles=frozenset({Role.ADMIN}))
REQUEST_ID = "req-guest-0001"


@contextmanager
def acting_as(principal: Principal, request_id: str) -> Iterator[None]:
    tokens = RequestContext.bind(request_id=request_id, principal=principal)
    try:
        yield
    finally:
        RequestContext.reset(tokens)


@pytest.fixture
def as_guest() -> Iterator[None]:
    with acting_as(GUEST, REQUEST_ID):
        yield


def _new_booking(**overrides: Any) -> BookingRow:
    values = {
        "guest_id": GUEST.user_id,
        "property_id": uuid7(),
        "room_type_id": uuid7(),
        "owner_account_id": uuid7(),
        "check_in": date(2026, 12, 3),
        "check_out": date(2026, 12, 5),
        "rooms": 1,
        "guests": 2,
        "booking_type": BookingType.PREPAID,
        "booking_status": BookingStatus.PENDING_PAYMENT,
        "total_amount_paisa": 800000,
        "nightly_rates": [
            {"date": "2026-12-03", "amount_paisa": 400000},
            {"date": "2026-12-04", "amount_paisa": 400000},
        ],
        "pricing_strategy": PricingStrategyCode.STANDARD,
        "cancellation_policy": CancellationPolicyCode.FLEXIBLE,
        "check_in_time": time(14, 0),
        "guest_name": "Rahul Sharma",
        "guest_email": "rahul@example.com",
        "guest_phone": "9876543210",
    }
    return BookingRow(**{**values, **overrides})


def _booking(**overrides: Any) -> BookingRow:
    row = _new_booking(**overrides)
    row.save(force_insert=True)
    return row


def _logs(row: BookingRow) -> list[BookingLogRow]:
    return list(BookingLogRow.objects.filter(log=row).order_by("creation_date", "id"))


def _raw(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


# --- audit columns ------------------------------------------------------------------------------------------


def test_insert_stamps_the_creator_from_the_request_context(as_guest: None) -> None:
    row = BookingRow.objects.get(pk=_booking().pk)

    assert (row.creation_by, row.status) == (GUEST.user_id, RecordStatus.ACTIVE)
    assert row.creation_date is not None
    assert (row.updation_by, row.updation_date) == (None, None)


def test_insert_outside_a_request_is_stamped_system() -> None:
    row = _booking()

    assert BookingRow.objects.get(pk=row.pk).creation_by == "system"
    assert [(log.creation_by, log.request_id) for log in _logs(row)] == [("system", None)]


def test_save_stamps_updation_and_appends_one_log_row_per_save(as_guest: None) -> None:
    row = _booking()

    with acting_as(ADMIN, "req-admin-0001"):
        loaded = BookingRow.objects.get(pk=row.pk)
        loaded.booking_status = BookingStatus.CONFIRMED
        loaded.save(update_fields=["booking_status"])

    fresh = BookingRow.objects.get(pk=row.pk)
    assert (fresh.creation_by, fresh.updation_by) == (GUEST.user_id, ADMIN.user_id)
    assert fresh.updation_date is not None and fresh.updation_date >= fresh.creation_date
    assert [(log.booking_status, log.creation_by, log.request_id) for log in _logs(row)] == [
        (BookingStatus.PENDING_PAYMENT, GUEST.user_id, REQUEST_ID),
        (BookingStatus.CONFIRMED, ADMIN.user_id, "req-admin-0001"),
    ]
    assert {field.name for field in BookingLogRow._meta.concrete_fields}.isdisjoint({"updation_by", "updation_date"})


def test_queryset_update_stamps_and_logs_every_row_it_matched(as_guest: None) -> None:
    first, second = _booking(), _booking(guest_name="Priya Patel", guest_email="priya@example.com")

    # The filter no longer matches once the update has run, so the log rows must come from the captured ids.
    count = BookingRow.objects.filter(
        pk__in=[first.pk, second.pk], booking_status=BookingStatus.PENDING_PAYMENT
    ).update(booking_status=BookingStatus.EXPIRED, guest_phone="9123456789")

    assert count == 2
    for row in (first, second):
        fresh = BookingRow.objects.get(pk=row.pk)
        assert (fresh.booking_status, fresh.updation_by) == (BookingStatus.EXPIRED, GUEST.user_id)
        assert (fresh.guest_phone, fresh.masked_guest_phone) == ("9123456789", "******6789")
        [(raw_phone,)] = _raw('SELECT guest_phone FROM "booking"."booking" WHERE id = %s', row.pk)
        assert raw_phone.startswith("ENC_")
        logs = _logs(row)
        assert len(logs) == 2
        assert (logs[-1].booking_status, logs[-1].guest_phone, logs[-1].request_id) == (
            BookingStatus.EXPIRED,
            "9123456789",
            REQUEST_ID,
        )


def test_bulk_create_stamps_fills_companions_and_logs_each_row(as_guest: None) -> None:
    created = BookingRow.objects.bulk_create(
        [_new_booking(), _new_booking(guest_name="Priya Patel", guest_email="priya@example.com")]
    )

    for row in created:
        fresh = BookingRow.objects.get(pk=row.pk)
        assert fresh.creation_by == GUEST.user_id
        assert fresh.masked_guest_email in {"r****@example.com", "p****@example.com"}
        assert [log.request_id for log in _logs(row)] == [REQUEST_ID]


def test_a_failed_log_write_rolls_back_the_save(as_guest: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_log(instance: Any) -> None:
        raise RuntimeError("log write failed")

    monkeypatch.setattr(models_base, "add_log_model", broken_log)
    with pytest.raises(RuntimeError), transaction.atomic():  # the caller's savepoint, as a repository has (LLD §2.5)
        _booking(guest_id="guest-rollback")

    assert not BookingRow.objects.filter(guest_id="guest-rollback").exists()


def test_repository_writes_log_every_state_change_of_a_hold(as_guest: None) -> None:
    inventory = get_inventory_api()
    room_type_id = uuid7()
    today = SystemClock().today()
    inventory.provision(room_type_id, total_rooms=2)

    hold = inventory.hold(
        room_type_id=room_type_id,
        stay=DateRange(today + timedelta(days=3), today + timedelta(days=5)),
        rooms=1,
        reference="booking-ref-1",
        expires_at=None,
    )
    inventory.confirm_hold(hold.hold_id)
    inventory.release_hold(hold.hold_id, "CANCELLED")

    logs = InventoryHoldLog.objects.filter(log_id=hold.hold_id).order_by("creation_date", "id")
    assert [(log.hold_status, log.creation_by, log.request_id) for log in logs] == [
        (HoldStatus.HELD, GUEST.user_id, REQUEST_ID),
        (HoldStatus.CONFIRMED, GUEST.user_id, REQUEST_ID),
        (HoldStatus.RELEASED, GUEST.user_id, REQUEST_ID),
    ]


# --- PII at rest --------------------------------------------------------------------------------------------


def test_guest_pii_is_ciphertext_in_the_live_and_log_tables_and_plaintext_in_the_orm(as_guest: None) -> None:
    row = _booking()

    [(email, name, phone)] = _raw(
        'SELECT guest_email, guest_name, guest_phone FROM "booking"."booking" WHERE id = %s', row.pk
    )
    [(log_email,)] = _raw('SELECT guest_email FROM "booking"."booking_log" WHERE log_id = %s', row.pk)

    for stored in (email, name, phone, log_email):
        assert stored.startswith("ENC_") and "rahul" not in stored.lower() and "3210" not in stored
    assert log_email != email  # encrypted again for the log row, with a fresh nonce
    for stored in (email, log_email):  # same column name, so the same associated data on both tables
        assert get_pii_cipher().decrypt(stored, aad="EMAIL|guest_email") == "rahul@example.com"
    assert BookingRow.objects.get(pk=row.pk).guest_email == "rahul@example.com"
    assert BookingLogRow.objects.get(log=row).guest_email == "rahul@example.com"


def test_masked_companions_are_filled_on_live_and_log_rows(as_guest: None) -> None:
    row = _booking()

    expected = ("R**** S*****", "r****@example.com", "******3210")
    for stored in (BookingRow.objects.get(pk=row.pk), BookingLogRow.objects.get(log=row)):
        assert (stored.masked_guest_name, stored.masked_guest_email, stored.masked_guest_phone) == expected


def test_owner_contact_email_is_found_by_its_blind_index_only(as_guest: None) -> None:
    account = OwnerAccountRow(
        display_name="Heritage Stays",
        owner_user_id="owner-7",
        contact_email="Owner@Heritage.example",
        contact_phone="9123456780",
    )
    account.save(force_insert=True)

    digest = get_blind_indexer().digest(" owner@HERITAGE.example ", PiType.EMAIL)  # normalised: strip + lower
    found = OwnerAccountRow.objects.get(contact_email_hash=digest)
    [(raw_email,)] = _raw('SELECT contact_email FROM "catalog"."owner_account" WHERE id = %s', account.pk)

    assert found.pk == account.pk
    assert (found.contact_email, found.masked_contact_email) == ("Owner@Heritage.example", "O****@Heritage.example")
    assert found.masked_contact_phone == "******6780"
    assert raw_email.startswith("ENC_") and "heritage" not in raw_email.lower()
    with pytest.raises(FieldError):  # ciphertext cannot be compared; lookups go through the blind index
        OwnerAccountRow.objects.filter(contact_email="Owner@Heritage.example").exists()
    duplicate = OwnerAccountRow(
        display_name="Copycat", owner_user_id="owner-8", contact_email="OWNER@heritage.example", contact_phone="9123456781"
    )
    with pytest.raises(IntegrityError), transaction.atomic():  # UNIQUE contact_email_hash on the live table
        duplicate.save(force_insert=True)


def test_encrypted_columns_refuse_value_lookups(as_guest: None) -> None:
    _booking()

    with pytest.raises(FieldError):
        BookingRow.objects.filter(guest_email="rahul@example.com").exists()
    assert not BookingRow.objects.filter(guest_email__isnull=True).exists()


# --- migrations ---------------------------------------------------------------------------------------------


def test_migrations_match_the_models() -> None:
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)  # exits non-zero on a pending change
