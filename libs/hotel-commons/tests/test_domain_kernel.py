"""Unit tests for the hotel_commons kernel (task 1-kernel).

Pure Python: no configured Django settings and no database. Covers money, date ranges, status codes, errors,
request context, events, enums, principals, logging, the Redis helpers, the settings toolkit and the test
doubles in hotel_commons.testing. Store tests skip until the security track adds the interface modules.
"""

import base64
import importlib
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

import hotel_commons.domain.money as money_module
from hotel_commons import Service
from hotel_commons import status_code as codes
from hotel_commons.context import RequestContext
from hotel_commons.domain.clock import BUSINESS_TIME_ZONE, SystemClock, business_date
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.enum import Enum
from hotel_commons.events.base import DomainEvent
from hotel_commons.events.in_process import InProcessEventBus
from hotel_commons.exceptions import (
    AppError,
    BusinessRuleViolation,
    Conflict,
    Forbidden,
    NotFound,
    PaymentDeclined,
    SecurityFailure,
    TooManyRequests,
    Unauthenticated,
    UpstreamFailure,
    ValidationFailed,
)
from hotel_commons.logging.config import build_logging_config
from hotel_commons.logging.filters import PiiRedactionFilter, RequestIdFilter, redact_pii
from hotel_commons.redis.client import redis_from_url, redis_key
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.principal import ANONYMOUS, SYSTEM_ACTOR, Principal, Role
from hotel_commons.settings import (
    decode_base64_key,
    load_dotenv,
    read_bool,
    read_choice,
    read_int,
    read_key,
    read_key_ring,
    validate_security_settings,
)
from hotel_commons.status_code import StatusCode
from hotel_commons.testing.clock import FixedClock
from hotel_commons.testing.events import RecordingEventPublisher
from hotel_commons.testing.unit_of_work import NoopUnitOfWork

LIB_ROOT = Path(__file__).resolve().parents[1]
GUEST = Principal(user_id="guest-1", roles=frozenset({Role.GUEST}))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# --- Money ---------------------------------------------------------------------------------------------------


def test_money_paisa_arithmetic():
    assert Money.of_rupees("4800.50").paisa == 480050
    assert Money(480050).rupees == Decimal("4800.50")
    assert Money(1000).times(Decimal("1.20")).paisa == 1200
    assert Money(999).percent(Decimal(50)).paisa == 500  # 499.5 rounds half up
    with pytest.raises(ValidationFailed):
        Money.of_rupees("1.005")


@pytest.mark.parametrize(
    "rupees",
    ["1.005", "0.105", "-1", "abc", "", "NaN", "sNaN", "Infinity", "1e30", "1E+999999999", True, 1.5, None],
)
def test_money_of_rupees_rejects_unsafe_input(rupees):
    with pytest.raises(ValidationFailed) as excinfo:
        Money.of_rupees(rupees)
    assert excinfo.value.http_status == 400
    assert "amount" in excinfo.value.errors


@pytest.mark.parametrize(
    ("rupees", "paisa"),
    [("4800.500", 480050), (4800, 480000), (Decimal("0.01"), 1), (" 12.5 ", 1250), ("0", 0)],
)
def test_money_of_rupees_accepts_exact_amounts(rupees, paisa):
    assert Money.of_rupees(rupees) == Money(paisa)


@pytest.mark.parametrize("paisa", [-1, True, 1.0, Decimal(1), "100", 2**63])
def test_money_rejects_non_int_negative_or_oversized_paisa(paisa):
    with pytest.raises(ValidationFailed):
        Money(paisa)


def test_money_rejects_unsupported_currency():
    with pytest.raises(ValidationFailed) as excinfo:
        Money(100, "USD")
    assert "currency" in excinfo.value.errors


def test_money_add_sub_and_currency_guard(monkeypatch):
    assert Money(100) + Money(50) == Money(150)
    assert Money(100) - Money(30) == Money(70)
    with pytest.raises(ValidationFailed):
        Money(30) - Money(100)  # never negative
    with pytest.raises(TypeError):
        Money(1) + 1
    monkeypatch.setattr(money_module, "SUPPORTED_CURRENCIES", frozenset({"INR", "USD"}))
    with pytest.raises(ValidationFailed) as excinfo:
        Money(100) + Money(100, "USD")
    assert "currency" in excinfo.value.errors


def test_money_times_percent_and_divide_round_half_up():
    assert Money(1001).times(3) == Money(3003)
    assert Money(5).times(Decimal("0.5")) == Money(3)  # 2.5 -> 3
    assert Money(1001).times(Decimal("1.25")) == Money(1251)  # 1251.25 -> 1251
    assert Money(1000).percent(0) == Money(0)
    assert Money(1000).percent(Decimal(100)) == Money(1000)
    assert Money(3).divide(2) == Money(2)  # 1.5 -> 2
    assert Money(10).divide(4) == Money(3)  # 2.5 -> 3
    assert Money(10).divide(3) == Money(3)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        (lambda m: m.times(1.2), TypeError),
        (lambda m: m.times(True), TypeError),
        (lambda m: m.times(Decimal("-1")), ValueError),
        (lambda m: m.times(Decimal("NaN")), ValueError),
        (lambda m: m.percent(Decimal(101)), ValueError),
        (lambda m: m.divide(0), ValueError),
        (lambda m: m.divide(2.0), TypeError),
    ],
)
def test_money_rejects_invalid_factors(operation, error):
    with pytest.raises(error):
        operation(Money(1000))


def test_money_zero_ordering_and_positivity():
    assert Money.zero() == Money(0)
    assert Money.zero().rupees == Decimal("0.00")
    assert str(Money(100).rupees) == "1.00"
    assert Money(1) < Money(2) <= Money(2) < Money(3)
    assert Money(3) > Money(2) >= Money(2)
    assert sorted([Money(3), Money(1), Money(2)]) == [Money(1), Money(2), Money(3)]
    assert Money(1).is_positive() and not Money.zero().is_positive()
    with pytest.raises(TypeError):
        _ = Money(1) < 2
    assert len({Money(5), Money(5)}) == 1


# --- DateRange -----------------------------------------------------------------------------------------------


def test_date_range_nights_dates_and_overlap():
    stay = DateRange(date(2026, 11, 20), date(2026, 11, 22))
    assert stay.nights == 2
    assert stay.dates() == (date(2026, 11, 20), date(2026, 11, 21))
    assert stay.overlaps(DateRange(date(2026, 11, 21), date(2026, 11, 23)))
    assert not stay.overlaps(DateRange(date(2026, 11, 22), date(2026, 11, 24)))  # check-out day is free


@pytest.mark.parametrize("end", [date(2026, 11, 19), date(2026, 11, 20)])
def test_date_range_rejects_inverted_or_empty(end):
    with pytest.raises(ValidationFailed) as excinfo:
        DateRange(date(2026, 11, 20), end)
    assert excinfo.value.errors == {"dates": "check_out must be after check_in"}


def test_date_range_rejects_datetimes():
    with pytest.raises(TypeError):
        DateRange(datetime(2026, 11, 20, tzinfo=UTC), date(2026, 11, 22))


# --- Clock and test clock ------------------------------------------------------------------------------------


def test_fixed_clock_today_uses_business_time_zone_and_advances():
    clock = FixedClock(datetime(2026, 11, 20, 20, 0, tzinfo=UTC))  # 01:30 on the 21st in Asia/Kolkata
    assert clock.now() == datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert clock.today() == date(2026, 11, 21)
    clock.advance(timedelta(hours=1))
    assert clock.now() == datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 11, 20, 20, 0))


def test_system_clock_is_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is UTC
    assert BUSINESS_TIME_ZONE.key == "Asia/Kolkata"
    assert business_date(datetime(2026, 1, 1, 18, 30, tzinfo=UTC)) == date(2026, 1, 2)
    with pytest.raises(ValueError):
        business_date(datetime(2026, 1, 1))


# --- Status codes and errors ---------------------------------------------------------------------------------


def test_status_code_safe_render():
    assert StatusCode(1, "a {x} b").render() == "a {x} b"
    assert StatusCode(1, "a {x} b").render(x=5) == "a 5 b"
    assert StatusCode(1, "{a}-{b}").render(a=1) == "1-{b}"
    assert StatusCode(1, "plain").http_status == 400


@pytest.mark.parametrize(
    ("name", "code", "http_status"),
    [
        ("SUCCESS", 2100000, 200),
        ("VALIDATION_FAILED", 2100001, 400),
        ("UNAUTHENTICATED", 2100002, 401),
        ("TOKEN_EXPIRED", 2100003, 401),
        ("FORBIDDEN", 2100004, 403),
        ("FIELD_ACCESS_DENIED", 2100005, 403),
        ("RESOURCE_NOT_FOUND", 2100006, 404),
        ("RATE_LIMITED", 2100007, 429),
        ("IDEMPOTENCY_KEY_REQUIRED", 2100008, 400),
        ("IDEMPOTENCY_KEY_INVALID", 2100009, 400),
        ("IDEMPOTENCY_IN_PROGRESS", 2100010, 409),
        ("IDEMPOTENCY_KEY_REUSED", 2100011, 422),
        ("PAYLOAD_NOT_ENCRYPTED", 2100012, 400),
        ("PAYLOAD_DECRYPTION_FAILED", 2100013, 400),
        ("SECURITY_ERROR", 2100014, 500),
        ("INTERNAL_ERROR", 2100015, 500),
        ("LOG_MODEL_NOT_FOUND", 2100016, 500),
        ("METHOD_NOT_ALLOWED", 2100017, 405),
        ("NOT_FOUND", 2100018, 404),
        ("RESOURCE_BUSY", 2100019, 409),
        ("SERVICE_TIMEOUT", 2100020, 503),
    ],
)
def test_common_status_catalogue(name, code, http_status):
    status = getattr(codes, name)
    assert (status.code, status.http_status) == (code, http_status)


def test_common_status_codes_are_unique():
    catalogue = [value for value in vars(codes).values() if isinstance(value, StatusCode)]
    assert len(catalogue) == 21
    assert len({status.code for status in catalogue}) == len(catalogue)
    assert codes.SERVICE_CODE == 21


def test_app_error_message_params_and_http_status():
    error = NotFound(codes.RESOURCE_NOT_FOUND, resource="Booking")
    assert isinstance(error, AppError)
    assert error.message == str(error) == "Booking not found"
    assert error.http_status == 404
    assert error.params == {"resource": "Booking"}
    assert error.errors is None
    assert "2100006" in repr(error)
    failed = ValidationFailed(codes.VALIDATION_FAILED, errors={"guests": "must be at least 1"})
    assert (failed.http_status, failed.errors) == (400, {"guests": "must be at least 1"})


def test_http_status_always_comes_from_the_status_code():
    refund_failed = StatusCode(2106007, "Refund could not be processed, please retry", 502)
    assert UpstreamFailure(refund_failed).http_status == 502
    for alias in (
        ValidationFailed,
        Unauthenticated,
        PaymentDeclined,
        Forbidden,
        NotFound,
        Conflict,
        BusinessRuleViolation,
        SecurityFailure,
        UpstreamFailure,
    ):
        assert issubclass(alias, AppError)
        assert alias(codes.FORBIDDEN).http_status == 403


def test_too_many_requests_carries_whole_retry_after_seconds():
    error = TooManyRequests(codes.RATE_LIMITED, retry_after_seconds=12.2)
    assert error.retry_after_seconds == 13
    assert error.message == "Too many requests, retry after 13s"
    assert error.http_status == 429
    assert TooManyRequests(codes.RATE_LIMITED, retry_after_seconds=-3).retry_after_seconds == 0


# --- Principal and request context ---------------------------------------------------------------------------


def test_principal_has_any_role():
    dual = Principal(user_id="u-1", roles=frozenset({Role.GUEST, Role.OWNER}))
    assert dual.has_any_role(Role.OWNER)
    assert dual.has_any_role(Role.ADMIN, Role.GUEST)
    assert not dual.has_any_role(Role.ADMIN)
    assert not dual.has_any_role()
    assert not ANONYMOUS.has_any_role(Role.GUEST)
    assert not Principal(user_id="u-2", roles=frozenset({Role.ADMIN}), is_authenticated=False).has_any_role(Role.ADMIN)


def test_principal_normalises_and_validates_roles():
    assert Principal(user_id="u-1", roles={"GUEST"}).roles == frozenset({Role.GUEST})
    with pytest.raises(ValueError):
        Principal(user_id="u-1", roles=frozenset({"ROOT"}))


def test_request_context_defaults_bind_and_reset():
    assert RequestContext.request_id() is None
    assert RequestContext.principal() is ANONYMOUS
    assert RequestContext.actor() == SYSTEM_ACTOR == "system"
    tokens = RequestContext.bind(request_id="req-1", principal=GUEST)
    try:
        assert RequestContext.request_id() == "req-1"
        assert RequestContext.principal() is GUEST
        assert RequestContext.actor() == "guest-1"
    finally:
        RequestContext.reset(tokens)
    assert RequestContext.request_id() is None
    assert RequestContext.actor() == "system"


def test_request_context_reset_undoes_later_rebinds():
    outer = RequestContext.bind(request_id="req-1", principal=ANONYMOUS)
    try:
        inner = RequestContext.bind(principal=GUEST)  # e.g. the JWT middleware rebinding the caller
        assert RequestContext.request_id() == "req-1"
        assert RequestContext.actor() == "guest-1"
        RequestContext.reset(inner)
        assert RequestContext.actor() == "system"
        RequestContext.bind(principal=GUEST)  # a rebind nobody resets ...
    finally:
        RequestContext.reset(outer)
    assert RequestContext.principal() is ANONYMOUS  # ... is still undone by the outer reset
    assert RequestContext.bind() == []


# --- Events --------------------------------------------------------------------------------------------------


class _Booked(DomainEvent):
    pass


class _Cancelled(DomainEvent):
    pass


def test_in_process_bus_runs_matching_handlers_in_subscription_order():
    bus = InProcessEventBus()
    calls = []
    bus.subscribe(_Booked, lambda event: calls.append(("first", event)))
    bus.subscribe(_Cancelled, lambda event: calls.append(("cancelled", event)))
    bus.subscribe(DomainEvent, lambda event: calls.append(("any", event)))
    bus.subscribe(_Booked, lambda event: calls.append(("second", event)))
    event = _Booked()
    bus.publish(event)
    assert calls == [("first", event), ("any", event), ("second", event)]


def test_in_process_bus_propagates_handler_errors_and_stops():
    bus = InProcessEventBus()
    calls = []

    def failing(event):
        raise RuntimeError("projection failed")

    bus.subscribe(_Booked, failing)
    bus.subscribe(_Booked, calls.append)
    with pytest.raises(RuntimeError, match="projection failed"):
        bus.publish(_Booked())
    assert calls == []


def test_in_process_bus_ignores_duplicate_subscriptions():
    bus = InProcessEventBus()
    calls = []
    bus.subscribe(_Booked, calls.append)
    bus.subscribe(_Booked, calls.append)
    bus.publish(_Booked())
    assert len(calls) == 1


def test_domain_event_defaults():
    event = _Booked()
    assert uuid.UUID(event.event_id).version == 7
    assert event.occurred_at.tzinfo is UTC
    assert event.event_type == "_Booked"
    assert _Booked().event_id != event.event_id


def test_recording_event_publisher_keeps_order_and_filters_by_type():
    publisher = RecordingEventPublisher()
    booked, cancelled = _Booked(), _Cancelled()
    publisher.publish(booked)
    publisher.publish(cancelled)
    assert publisher.events == [booked, cancelled]
    assert publisher.of_type(_Cancelled) == [cancelled]


# --- Enums ---------------------------------------------------------------------------------------------------


class _Colour(Enum):
    RED = 1
    DARK_BLUE = 2
    __labels__ = {1: "Rouge"}


def test_enum_int_str_and_labels():
    assert int(_Colour.RED) == 1
    assert str(_Colour.DARK_BLUE) == "2"
    assert _Colour(2) is _Colour.DARK_BLUE
    assert _Colour.RED.label == "Rouge"
    assert _Colour.DARK_BLUE.label == "Dark Blue"
    assert _Colour.from_label("Dark Blue") is _Colour.DARK_BLUE
    with pytest.raises(ValueError):
        _Colour.from_label("Green")


@pytest.mark.parametrize(("value", "error"), [("red", TypeError), (True, TypeError), (1.0, TypeError),
                                              (-1, ValueError), (32768, ValueError)])
def test_enum_values_must_fit_smallint(value, error):
    with pytest.raises(error):
        Enum("Bad", {"A": value})


def test_enum_rejects_duplicate_values():
    with pytest.raises(ValueError, match="B"):
        Enum("Duplicated", {"A": 1, "B": 1})


def test_pi_type_members():
    assert issubclass(PiType, Enum)
    assert [(member.name, member.value) for member in PiType] == [
        ("NAME", 1),
        ("EMAIL", 2),
        ("PHONE", 3),
        ("UPI_VPA", 4),
        ("OTHER", 9),
    ]


# --- Service registry ----------------------------------------------------------------------------------------


def test_service_registry_sets_class_level_name(monkeypatch):
    monkeypatch.setattr(Service, "name", None)
    Service(name=" Hotel ")
    assert Service.name == "hotel"
    with pytest.raises(ValueError):
        Service(name="unknown")
    assert Service.name == "hotel"


# --- Logging -------------------------------------------------------------------------------------------------


def _record(msg, args=(), exc_info=None):
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, exc_info)


def test_pii_redaction_masks_email_and_card_number():
    record = _record("guest %s paid with %s", ("rahul@gmail.com", "4242424242424242"))
    assert PiiRedactionFilter().filter(record) is True
    assert record.getMessage() == "guest r****@gmail.com paid with ************4242"


def test_pii_redaction_covers_phone_cvv_upi_and_mapping_args():
    record = _record("payload %(body)s", ({"body": {"cvv": "123", "phone": "9876543210", "vpa": "rahul@okaxis"}},))
    PiiRedactionFilter().filter(record)
    assert record.getMessage() == "payload {'cvv': '***', 'phone': '******3210', 'vpa': 'r****@okaxis'}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"cvv": "4321"}', '{"cvv": "***"}'),
        ("card 4242 4242 4242 4242 ok", "card ************4242 ok"),
        ("call +919876543210", "call ******3210"),
        ("order 2026-11-20 for 2 guests", "order 2026-11-20 for 2 guests"),
    ],
)
def test_redact_pii_patterns(text, expected):
    assert redact_pii(text) == expected
    assert redact_pii(expected) == expected  # masks are stable


def test_redact_pii_keeps_uuids_whole():
    # A UUIDv7's leading digits pass for a card number ("17904-2851-7046"), and so may a digit-only tail.
    text = "booking 01a17904-2851-7046-a3f0-df0c3d0e88ca hold 0192f5c4-0000-7000-8000-987654321012 card 4242424242424242"
    assert redact_pii(text) == (
        "booking 01a17904-2851-7046-a3f0-df0c3d0e88ca hold 0192f5c4-0000-7000-8000-987654321012 card ************4242"
    )


def test_pii_redaction_masks_exception_text_and_survives_bad_args():
    try:
        raise ValueError("duplicate contact rahul@gmail.com")
    except ValueError:
        record = _record("insert failed", exc_info=sys.exc_info())
    PiiRedactionFilter().filter(record)
    assert "rahul@gmail.com" not in record.exc_text
    assert "r****@gmail.com" in record.exc_text
    broken = _record("%s and %s", ("rahul@gmail.com",))
    assert PiiRedactionFilter().filter(broken) is True
    assert "rahul@gmail.com" not in repr(broken.args)


def test_request_id_filter_adds_request_id_and_actor():
    outside = _record("outside")
    RequestIdFilter().filter(outside)
    assert (outside.request_id, outside.actor) == ("-", "system")
    tokens = RequestContext.bind(request_id="req-9", principal=GUEST)
    try:
        inside = _record("inside")
        RequestIdFilter().filter(inside)
    finally:
        RequestContext.reset(tokens)
    assert (inside.request_id, inside.actor) == ("req-9", "guest-1")


def test_build_logging_config_wires_filters_and_quiets_sql():
    config = build_logging_config("debug")
    assert config["root"]["level"] == "DEBUG"
    assert config["handlers"]["console"]["filters"] == ["request_context", "pii_redaction"]
    assert config["loggers"]["django.db.backends"]["level"] == "WARNING"
    targets = {name: spec["()"] for name, spec in config["filters"].items()}
    resolved = {name: getattr(importlib.import_module(path.rpartition(".")[0]), path.rpartition(".")[2])
                for name, path in targets.items()}
    assert resolved == {"request_context": RequestIdFilter, "pii_redaction": PiiRedactionFilter}
    with pytest.raises(ValueError):
        build_logging_config("LOUD")


# --- Redis helpers -------------------------------------------------------------------------------------------


def test_redis_key_uses_library_prefix():
    assert redis_key("rl", "v1", "default", "u:guest-1") == "hb:rl:v1:default:u:guest-1"
    assert redis_key("idem", 1) == "hb:idem:1"
    with pytest.raises(ValueError):
        redis_key()


def test_fakeredis_url_yields_one_process_wide_client():
    client = redis_from_url("fakeredis://")
    assert client is redis_from_url("fakeredis://")
    client.set("hb:test:kernel", "value")
    try:
        assert client.get("hb:test:kernel") == "value"  # decode_responses=True
    finally:
        client.delete("hb:test:kernel")


def test_real_redis_url_uses_short_timeouts_without_connecting():
    client = redis_from_url("redis://localhost:6399/0")
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["socket_timeout"], kwargs["socket_connect_timeout"], kwargs["decode_responses"]) == (0.5, 0.5, True)


# --- Settings toolkit ----------------------------------------------------------------------------------------


def test_read_key_accepts_standard_and_urlsafe_base64(monkeypatch):
    raw = bytes(range(250, 256)) * 6  # forces '+' / '/' in standard base64
    monkeypatch.setenv("TEST_KEY", base64.b64encode(raw).decode())
    assert read_key("TEST_KEY") == raw
    monkeypatch.setenv("TEST_KEY", base64.urlsafe_b64encode(raw).decode().rstrip("="))
    assert read_key("TEST_KEY") == raw
    monkeypatch.delenv("TEST_KEY")
    assert read_key("TEST_KEY") is None
    monkeypatch.setenv("TEST_KEY", "not base64 !")
    with pytest.raises(ImproperlyConfigured, match="TEST_KEY"):
        read_key("TEST_KEY")
    assert decode_base64_key(_b64(b"k" * 32)) == b"k" * 32


def test_read_key_ring_decodes_json_object(monkeypatch):
    monkeypatch.setenv("TEST_RING", json.dumps({"k1": _b64(b"a" * 32), "k2": _b64(b"b" * 32)}))
    assert read_key_ring("TEST_RING") == {"k1": b"a" * 32, "k2": b"b" * 32}
    monkeypatch.delenv("TEST_RING")
    assert read_key_ring("TEST_RING") == {}
    for malformed in ("not json", '["k1"]', '{"k1": 7}', '{"k1": "@@@"}'):
        monkeypatch.setenv("TEST_RING", malformed)
        with pytest.raises(ImproperlyConfigured, match="TEST_RING"):
            read_key_ring("TEST_RING")


def test_typed_env_readers(monkeypatch):
    monkeypatch.setenv("TEST_FLAG", "Yes")
    assert read_bool("TEST_FLAG", default=False) is True
    monkeypatch.setenv("TEST_FLAG", "0")
    assert read_bool("TEST_FLAG", default=True) is False
    monkeypatch.setenv("TEST_FLAG", "maybe")
    with pytest.raises(ImproperlyConfigured):
        read_bool("TEST_FLAG", default=False)
    monkeypatch.setenv("TEST_INT", "-1")
    with pytest.raises(ImproperlyConfigured, match=">= 0"):
        read_int("TEST_INT", 5, minimum=0)
    monkeypatch.delenv("TEST_INT")
    assert read_int("TEST_INT", 5, minimum=0) == 5
    monkeypatch.setenv("TEST_MODE", "ENFORCED")
    assert read_choice("TEST_MODE", "optional", ("off", "optional", "enforced")) == "enforced"
    monkeypatch.setenv("TEST_MODE", "sometimes")
    with pytest.raises(ImproperlyConfigured, match="TEST_MODE"):
        read_choice("TEST_MODE", "optional", ("off", "optional", "enforced"))


def test_load_dotenv_only_fills_gaps(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nTEST_DOTENV_A=from-file\nexport TEST_DOTENV_B='quoted value'\n"
        'TEST_DOTENV_C={"k1": "abc="}\nnot a pair\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_DOTENV_A", "from-shell")
    for name in ("TEST_DOTENV_B", "TEST_DOTENV_C"):
        monkeypatch.setenv(name, "")  # registers the variable, so teardown removes what load_dotenv sets
        monkeypatch.delenv(name)
    load_dotenv(env_file)
    assert os.environ["TEST_DOTENV_A"] == "from-shell"
    assert os.environ["TEST_DOTENV_B"] == "quoted value"
    assert os.environ["TEST_DOTENV_C"] == '{"k1": "abc="}'
    load_dotenv(tmp_path / "missing.env")  # an absent file is a no-op


def _config(base_dir: Path, **overrides) -> SimpleNamespace:
    values = {
        "BASE_DIR": base_dir,
        "ENVIRONMENT": "production",
        "SECRET_KEY": "production-secret",
        "JWT_SIGNING_KEY": b"j" * 32,
        "PII_ENCRYPTION_KEYS": {"k1": b"p" * 32},
        "PII_ACTIVE_KEY_ID": "k1",
        "PII_BLIND_INDEX_KEY": b"b" * 32,
        "IDEMPOTENCY_FINGERPRINT_KEY": b"f" * 32,
        "API_ENCRYPTION_MODE": "optional",
        "API_RSA_PRIVATE_KEY_PATH": str(base_dir / "keys" / "rsa.pem"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validate_security_settings_accepts_a_sound_configuration(tmp_path):
    validate_security_settings(_config(tmp_path))


def test_validate_security_settings_reports_every_problem(tmp_path):
    config = _config(
        tmp_path,
        JWT_SIGNING_KEY=b"short",
        PII_ENCRYPTION_KEYS={"k1": b"p" * 16},
        PII_ACTIVE_KEY_ID="k2",
        PII_BLIND_INDEX_KEY=None,
        IDEMPOTENCY_FINGERPRINT_KEY=b"",
    )
    with pytest.raises(ImproperlyConfigured) as excinfo:
        validate_security_settings(config)
    message = str(excinfo.value)
    for name in ("JWT_SIGNING_KEY", "PII_ENCRYPTION_KEYS['k1']", "PII_ACTIVE_KEY_ID", "PII_BLIND_INDEX_KEY",
                 "IDEMPOTENCY_FINGERPRINT_KEY"):
        assert name in message
    assert "jjjj" not in message and "pppp" not in message  # never echoes key material


def test_validate_refuses_committed_dev_keys_outside_local(tmp_path):
    dev_ring = json.dumps({"k1": _b64(b"e" * 32)})
    (tmp_path / ".env.example").write_text(
        f"DJANGO_SECRET_KEY=dev-secret\nJWT_SIGNING_KEY={_b64(b'd' * 32)}\nPII_ENCRYPTION_KEYS={dev_ring}\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, SECRET_KEY="dev-secret", JWT_SIGNING_KEY=b"d" * 32,
                     PII_ENCRYPTION_KEYS={"k1": b"e" * 32})
    with pytest.raises(ImproperlyConfigured) as excinfo:
        validate_security_settings(config)
    for name in ("SECRET_KEY", "JWT_SIGNING_KEY", "PII_ENCRYPTION_KEYS"):
        assert name in str(excinfo.value)
    config.ENVIRONMENT = "Local"
    validate_security_settings(config)


class _SettingsWithoutSecretKey(SimpleNamespace):
    """Mimics django.conf.settings, which raises rather than return an empty SECRET_KEY."""

    @property
    def SECRET_KEY(self):
        raise ImproperlyConfigured("The SECRET_KEY setting must not be empty.")


def test_validate_tolerates_settings_that_refuse_an_empty_secret_key(tmp_path):
    values = vars(_config(tmp_path))
    del values["SECRET_KEY"]
    validate_security_settings(_SettingsWithoutSecretKey(**values))


# --- base_settings (imported in a clean subprocess: it reads the environment at import) ----------------------

_SETTINGS_PROBE = """
import json
namespace = {}
exec("from hotel_commons.settings.base_settings import *", namespace)
import hotel_commons.settings.base_settings as s
db = s.DATABASES["default"]
print(json.dumps({
    "leaked_names": sorted(name for name in namespace if name != "__builtins__" and not name.isupper()),
    "middleware": s.MIDDLEWARE,
    "installed_apps": s.INSTALLED_APPS,
    "db": [db["ENGINE"], db["CONN_MAX_AGE"], db["CONN_HEALTH_CHECKS"], db["ATOMIC_REQUESTS"], db["OPTIONS"]],
    "rate_limit_policies": s.RATE_LIMIT_POLICIES,
    "bypass": sorted(s.API_ENCRYPTION_BYPASS_PATHS),
    "environment": s.ENVIRONMENT,
    "allowed_hosts": s.ALLOWED_HOSTS,
    "api_version": s.API_VERSION,
    "max_stay_nights": s.MAX_STAY_NIGHTS,
    "encryption_mode": s.API_ENCRYPTION_MODE,
    "flags": [s.APPEND_SLASH, s.USE_TZ, s.TIME_ZONE, s.DEBUG],
    "jwt_key_len": len(s.JWT_SIGNING_KEY) if s.JWT_SIGNING_KEY else None,
    "pii_ring": {key_id: len(key) for key_id, key in s.PII_ENCRYPTION_KEYS.items()},
    "rsa_path": s.API_RSA_PRIVATE_KEY_PATH,
    "rest_framework": s.REST_FRAMEWORK,
    "spectacular": s.SPECTACULAR_SETTINGS,
    "logging_root": s.LOGGING["root"]["level"],
}))
"""


def _run_settings_probe(base_dir: Path, **env: str) -> subprocess.CompletedProcess:
    clean_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(LIB_ROOT), "DJANGO_BASE_DIR": str(base_dir)}
    return subprocess.run(
        [sys.executable, "-c", _SETTINGS_PROBE],
        env={**clean_env, **env},
        cwd=base_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_base_settings_defines_the_contract(tmp_path):
    (tmp_path / ".env").write_text("MAX_STAY_NIGHTS=12\nAPI_VERSION=v9\n", encoding="utf-8")
    result = _run_settings_probe(
        tmp_path,
        API_VERSION="v1",
        JWT_SIGNING_KEY=_b64(b"j" * 32),
        PII_ENCRYPTION_KEYS=json.dumps({"k1": _b64(b"p" * 32)}),
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["leaked_names"] == []
    assert settings["middleware"] == [
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "hotel_commons.middlewares.request_context.RequestContextMiddleware",
        "hotel_commons.middlewares.exception_handling.ExceptionHandlingMiddleware",
        "hotel_commons.middlewares.api_encryption.ApiEncryptionMiddleware",
        "hotel_commons.middlewares.authentication.JWTAuthenticationMiddleware",
    ]
    assert settings["installed_apps"] == [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.staticfiles",
        "rest_framework",
        "drf_spectacular",
        "hotel_commons",
    ]
    assert settings["db"] == [
        "hotel_commons.db.backends.postgresql",
        60,
        True,
        False,
        {"options": "-c lock_timeout=2000 -c statement_timeout=5000"},
    ]
    assert settings["rate_limit_policies"] == {
        "default": {"limit": 120, "window_seconds": 60, "fail_open": True},
        "search": {"limit": 60, "window_seconds": 60, "fail_open": True},
        "catalog_write": {"limit": 30, "window_seconds": 60, "fail_open": True},
        "booking_write": {"limit": 20, "window_seconds": 60, "fail_open": True},
        "payment": {"limit": 10, "window_seconds": 60, "fail_open": False},
        "encrypted": {"limit": 300, "window_seconds": 60, "fail_open": True},
    }
    assert settings["bypass"] == [
        "/hotel/v1/dev/tokens",
        "/hotel/v1/docs",
        "/hotel/v1/health",
        "/hotel/v1/schema",
        "/hotel/v1/security/public-key",
    ]
    assert settings["environment"] == "production"
    assert settings["allowed_hosts"] == []
    assert (settings["api_version"], settings["max_stay_nights"]) == ("v1", 12)  # real env wins over .env
    assert settings["encryption_mode"] == "optional"
    assert settings["flags"] == [False, True, "Asia/Kolkata", False]
    assert (settings["jwt_key_len"], settings["pii_ring"]) == (32, {"k1": 32})
    assert settings["rsa_path"] == str(tmp_path / "secrets" / "dev" / "api_rsa_private.pem")
    rest = settings["rest_framework"]
    assert rest["EXCEPTION_HANDLER"] == "hotel_commons.api.exception_handler.envelope_exception_handler"
    assert rest["DEFAULT_RENDERER_CLASSES"] == ["rest_framework.renderers.JSONRenderer"]
    assert rest["DEFAULT_PARSER_CLASSES"] == ["rest_framework.parsers.JSONParser"]
    assert rest["UNAUTHENTICATED_USER"] is None
    assert rest["DEFAULT_METADATA_CLASS"] is None  # OPTIONS answers 405 instead of failing in DRF metadata
    spectacular = settings["spectacular"]
    assert spectacular["TITLE"] == "Hotel Booking Service API"
    assert spectacular["SERVE_PERMISSIONS"] == ["rest_framework.permissions.AllowAny"]
    assert (spectacular["SERVE_AUTHENTICATION"], spectacular["COMPONENT_SPLIT_REQUEST"]) == ([], True)
    assert settings["logging_root"] == "INFO"


@pytest.mark.parametrize(
    ("env", "options"),
    [
        ({"DB_LOCK_TIMEOUT_MS": "500", "DB_STATEMENT_TIMEOUT_MS": "9000"},
         {"options": "-c lock_timeout=500 -c statement_timeout=9000"}),
        ({"DB_LOCK_TIMEOUT_MS": "0"}, {"options": "-c statement_timeout=5000"}),
        ({"DB_LOCK_TIMEOUT_MS": "0", "DB_STATEMENT_TIMEOUT_MS": "0"}, {}),  # how docker/entrypoint.sh migrates
    ],
)
def test_base_settings_database_timeouts_come_from_the_environment(tmp_path, env, options):
    result = _run_settings_probe(tmp_path, **env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["db"][4] == options


def test_base_settings_refuses_a_negative_database_timeout(tmp_path):
    result = _run_settings_probe(tmp_path, DB_STATEMENT_TIMEOUT_MS="-1")
    assert result.returncode != 0
    assert "DB_STATEMENT_TIMEOUT_MS must be >= 0" in result.stderr


def test_base_settings_import_defers_missing_keys_to_startup_validation(tmp_path):
    result = _run_settings_probe(tmp_path, ENVIRONMENT="Local")
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert (settings["environment"], settings["jwt_key_len"], settings["pii_ring"]) == ("local", None, {})
    assert settings["allowed_hosts"] == ["*"]


@pytest.mark.parametrize(
    ("name", "value"),
    [("JWT_SIGNING_KEY", "*** not base64 ***"), ("API_ENCRYPTION_MODE", "sometimes"), ("TRUSTED_PROXY_COUNT", "-1")],
)
def test_base_settings_rejects_malformed_environment(tmp_path, name, value):
    result = _run_settings_probe(tmp_path, **{name: value})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and name in result.stderr


# --- Test doubles --------------------------------------------------------------------------------------------


def test_noop_unit_of_work_counts_and_never_swallows_errors():
    uow = NoopUnitOfWork()
    with uow as entered:
        assert entered is uow
    with pytest.raises(RuntimeError):
        with uow:
            raise RuntimeError("boom")
    assert (uow.enters, uow.commits, uow.rollbacks) == (2, 1, 1)


def test_store_and_token_modules_import_without_their_lazy_dependencies():
    stores = importlib.import_module("hotel_commons.testing.stores")
    tokens = importlib.import_module("hotel_commons.testing.tokens")
    assert set(stores.__all__) == {"InMemoryIdempotencyStore", "InMemoryRateLimiter"}
    assert callable(tokens.make_token) and callable(tokens.auth_header)
    with pytest.raises(AttributeError):
        getattr(stores, "NoSuchStore")


class _Ticker:
    """Manually advanced monotonic time source for the in-memory stores."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def test_in_memory_idempotency_store_semantics():
    interfaces = pytest.importorskip("hotel_commons.idempotency.interfaces")
    from hotel_commons.testing.stores import InMemoryIdempotencyStore

    ticker = _Ticker()
    store = InMemoryIdempotencyStore(now=ticker)
    assert isinstance(store, interfaces.IdempotencyStore)
    assert store.begin("k", "fp", 60).kind == "started"
    assert store.begin("k", "fp", 60).kind == "in_progress"
    assert store.begin("k", "other", 60).kind == "mismatch"
    body = {"booking_id": "b-1"}
    store.complete("k", "fp", interfaces.StoredResponse(http_status=201, body=body), 3600)
    body["booking_id"] = "mutated"
    replay = store.begin("k", "fp", 60)
    assert (replay.kind, replay.response) == ("replay", interfaces.StoredResponse(201, {"booking_id": "b-1"}))
    store.release("k")
    assert store.begin("k", "fp", 10).kind == "started"
    ticker.value += 11  # the in-progress lock expired
    assert store.begin("k", "fp", 10).kind == "started"


def test_in_memory_rate_limiter_sliding_window():
    interfaces = pytest.importorskip("hotel_commons.ratelimit.interfaces")
    from hotel_commons.testing.stores import InMemoryRateLimiter

    ticker = _Ticker()
    limiter = InMemoryRateLimiter(now=ticker)
    policy = interfaces.RateLimitPolicy(scope="payment", limit=2, window_seconds=60, fail_open=False)
    assert isinstance(limiter, interfaces.RateLimiter)
    first, second = limiter.hit("u:guest-1", policy), limiter.hit("u:guest-1", policy)
    assert (first.allowed, first.remaining, second.allowed, second.remaining) == (True, 1, True, 0)
    ticker.value += 10.5
    denied = limiter.hit("u:guest-1", policy)
    assert (denied.allowed, denied.remaining, denied.retry_after_seconds) == (False, 0, 50)
    assert limiter.hit("u:guest-2", policy).allowed  # buckets are per subject
    ticker.value += 50
    assert limiter.hit("u:guest-1", policy).allowed  # the window slid past both hits
