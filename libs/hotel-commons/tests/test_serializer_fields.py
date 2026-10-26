"""The API edge (LLD §2.10-§2.12, §3.7, §3.9, §3.14): Hotel*Field converters, field-level security,
the column converters behind them, and the error envelope.

Wire format: amounts are rupee strings ("4800.50") and enums are member names ("PREPAID"). Inside: amounts
are integer paisa and enums are members, stored as SMALLINT. Django is configured minimally here, with no
database: serializers need settings and the app registry, nothing more.
"""

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework"],
        USE_TZ=True,
        TIME_ZONE="Asia/Kolkata",
        REST_FRAMEWORK={"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"},  # as base_settings
    )
django.setup()

# DRF and drf-spectacular read settings while being imported, so these imports follow the setup above.
import datetime as dt  # noqa: E402
import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from decimal import Decimal  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from django.core.exceptions import ValidationError as DjangoValidationError  # noqa: E402
from django.db import models  # noqa: E402
from django.http import Http404, QueryDict  # noqa: E402
from django.urls import path  # noqa: E402
from drf_spectacular.generators import SchemaGenerator  # noqa: E402
from drf_spectacular.utils import extend_schema  # noqa: E402
from rest_framework import exceptions  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from hotel_commons import status_code as codes  # noqa: E402
from hotel_commons.api.exception_handler import envelope_exception_handler  # noqa: E402
from hotel_commons.api.pagination import PageRequest, page_data  # noqa: E402
from hotel_commons.api.response import api_response  # noqa: E402
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope  # noqa: E402
from hotel_commons.context import RequestContext  # noqa: E402
from hotel_commons.domain.money import Money  # noqa: E402
from hotel_commons.enum import Enum  # noqa: E402
from hotel_commons.exceptions import Forbidden, TooManyRequests, ValidationFailed  # noqa: E402
from hotel_commons.models.fields import EncryptedTextField, EnumField  # noqa: E402
from hotel_commons.security.pi_type import PiType  # noqa: E402
from hotel_commons.security.principal import ANONYMOUS, Principal, Role  # noqa: E402
from hotel_commons.serializers.base import BaseHotelModelSerializer, BaseHotelSerializer  # noqa: E402
from hotel_commons.serializers.fields import (  # noqa: E402
    HotelAmountField,
    HotelCharField,
    HotelCsvListField,
    HotelDateField,
    HotelDateTimeField,
    HotelEmailField,
    HotelEncryptedCharField,
    HotelEnumField,
    HotelIntegerField,
    HotelPhoneField,
    HotelTimeField,
    HotelUUIDField,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class StayType(Enum):
    PREPAID = 1
    PAY_AT_PROPERTY = 2
    __labels__ = {2: "Pay at property"}


class Colour(Enum):
    RED = 1


GUEST = Principal(user_id="guest-1", roles=frozenset({Role.GUEST}))
OTHER_GUEST = Principal(user_id="guest-2", roles=frozenset({Role.GUEST}))
OWNER = Principal(user_id="owner-1", roles=frozenset({Role.OWNER}))
ADMIN = Principal(user_id="admin-1", roles=frozenset({Role.ADMIN}))


def as_caller(principal: Principal) -> dict:
    """Serializer context as views build it: the request carries the authenticated principal."""
    return {"request": SimpleNamespace(principal=principal)}


def error_codes(error: exceptions.ValidationError) -> list[str]:
    return error.get_codes()


# --- HotelAmountField: rupees on the wire, integer paisa inside ------------------------------------------------


@pytest.mark.parametrize(
    ("wire", "paisa"),
    [
        ("4800.5", 480050),
        ("4800.50", 480050),
        ("4800.500", 480050),
        ("4800", 480000),
        (4800, 480000),
        (4800.5, 480050),
        (Decimal("4800.05"), 480005),
        (" 0.01 ", 1),
        ("0", 0),
    ],
)
def test_amount_parses_rupees_into_integer_paisa(wire, paisa):
    parsed = HotelAmountField().to_internal_value(wire)

    assert parsed == paisa
    assert type(parsed) is int


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        ("0.105", "max_decimal_places"),
        (0.1 + 0.2, "max_decimal_places"),
        ("-1", "negative"),
        (-1, "negative"),
        ("abc", "invalid"),
        ("NaN", "invalid"),
        (float("nan"), "invalid"),
        (float("inf"), "invalid"),
        (Decimal("Infinity"), "invalid"),
        ("1e3", "invalid"),
        ("1_000", "invalid"),
        ("", "invalid"),
        (True, "invalid"),
        (["4800"], "invalid"),
    ],
)
def test_amount_rejects_anything_but_a_non_negative_amount_with_two_decimals(wire, code):
    with pytest.raises(exceptions.ValidationError) as error:
        HotelAmountField().to_internal_value(wire)

    assert error_codes(error.value) == [code]


def test_amount_enforces_the_configured_range():
    field = HotelAmountField(min_rupees=Decimal("1"), max_rupees=Decimal("50000"))

    assert (field.to_internal_value("1"), field.to_internal_value("50000")) == (100, 5_000_000)
    for wire, code in [("0.99", "min_value"), ("50000.01", "max_value")]:
        with pytest.raises(exceptions.ValidationError) as error:
            field.to_internal_value(wire)
        assert error_codes(error.value) == [code]


def test_amount_rejects_values_that_overflow_a_bigint_column():
    field = HotelAmountField()

    assert field.to_internal_value("92233720368547758.07") == 2**63 - 1
    with pytest.raises(exceptions.ValidationError) as error:
        field.to_internal_value("92233720368547758.08")
    assert error_codes(error.value) == ["max_value"]


@pytest.mark.parametrize(
    ("paisa", "wire"),
    [(480000, "4800.00"), (480050, "4800.50"), (5, "0.05"), (0, "0.00"), (Money(480050), "4800.50")],
)
def test_amount_renders_paisa_as_rupees_with_two_decimals(paisa, wire):
    assert HotelAmountField().to_representation(paisa) == wire


@pytest.mark.parametrize("value", [4800.5, Decimal("4800.50"), "480050", True])
def test_amount_refuses_to_render_anything_but_paisa(value):
    with pytest.raises(TypeError):
        HotelAmountField().to_representation(value)


def test_amount_errors_are_reported_under_the_field_name():
    class PriceSerializer(BaseHotelSerializer):
        base_price = HotelAmountField()

    serializer = PriceSerializer(data={"base_price": "0.105"}, context=as_caller(OWNER))

    assert not serializer.is_valid()
    assert [detail.code for detail in serializer.errors["base_price"]] == ["max_decimal_places"]


# --- HotelEnumField: member names on the wire --------------------------------------------------------------------


def test_enum_parses_a_member_name():
    assert HotelEnumField(StayType).to_internal_value("PAY_AT_PROPERTY") is StayType.PAY_AT_PROPERTY


@pytest.mark.parametrize("wire", [1, "1", "prepaid", "UNKNOWN", True, None, ["PREPAID"]])
def test_enum_rejects_values_and_unknown_names_listing_the_allowed_names(wire):
    with pytest.raises(exceptions.ValidationError) as error:
        HotelEnumField(StayType).to_internal_value(wire)

    assert error_codes(error.value) == ["invalid_choice"]
    assert "PREPAID, PAY_AT_PROPERTY" in str(error.value.detail[0])


@pytest.mark.parametrize(
    ("value", "wire"), [(StayType.PAY_AT_PROPERTY, "PAY_AT_PROPERTY"), (1, "PREPAID")], ids=["member", "raw-int"]
)
def test_enum_renders_a_member_or_its_stored_int_as_the_name(value, wire):
    assert HotelEnumField(StayType).to_representation(value) == wire


def test_enum_refuses_to_render_a_member_of_another_enum():
    with pytest.raises(TypeError):
        HotelEnumField(StayType).to_representation(Colour.RED)


def test_enum_can_speak_another_label():
    field = HotelEnumField(StayType, label_name="label")

    assert field.to_internal_value("Pay at property") is StayType.PAY_AT_PROPERTY
    assert field.to_representation(1) == "Prepaid"
    with pytest.raises(exceptions.ValidationError):
        field.to_internal_value("PAY_AT_PROPERTY")


def test_enum_help_text_lists_the_names():
    assert "PREPAID, PAY_AT_PROPERTY" in HotelEnumField(StayType).help_text


# --- Other scalar fields ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("wire", [True, 4.0, 4.5, "4.0", "4.5", Decimal("4"), "four"])
def test_integer_field_rejects_booleans_floats_and_decimal_strings(wire):
    with pytest.raises(exceptions.ValidationError):
        HotelIntegerField().to_internal_value(wire)


@pytest.mark.parametrize(("wire", "number"), [(7, 7), ("7", 7), ("-3", -3)])
def test_integer_field_accepts_whole_numbers_and_query_strings(wire, number):
    assert HotelIntegerField().to_internal_value(wire) == number


def test_date_field_reads_and_writes_yyyy_mm_dd():
    field = HotelDateField()

    assert field.to_internal_value("2026-11-20") == dt.date(2026, 11, 20)
    assert field.to_representation(dt.date(2026, 11, 20)) == "2026-11-20"


@pytest.mark.parametrize("wire", ["20261120", "2026-W47-5", "2026-1-5", "20/11/2026", "2026-11-20T10:00:00"])
def test_date_field_rejects_every_other_format(wire):
    with pytest.raises(exceptions.ValidationError):
        HotelDateField().to_internal_value(wire)


def test_datetime_field_requires_an_offset_and_renders_utc():
    field = HotelDateTimeField()

    assert field.to_internal_value("2026-11-20T14:00:00+05:30") == dt.datetime(2026, 11, 20, 8, 30, tzinfo=dt.UTC)
    assert field.to_representation(dt.datetime(2026, 11, 20, 14, 0, tzinfo=IST)) == "2026-11-20T08:30:00Z"
    with pytest.raises(exceptions.ValidationError) as error:
        field.to_internal_value("2026-11-20T14:00:00")
    assert error_codes(error.value) == ["offset_required"]


def test_time_field_reads_and_writes_hh_mm():
    field = HotelTimeField()

    assert field.to_internal_value("14:00") == dt.time(14, 0)
    assert field.to_representation(dt.time(11, 0)) == "11:00"
    for wire in ["9:00", "14:00:00", "2pm"]:
        with pytest.raises(exceptions.ValidationError):
            field.to_internal_value(wire)


def test_email_field_lower_cases():
    assert HotelEmailField().run_validation(" Rahul@Example.COM ") == "rahul@example.com"


@pytest.mark.parametrize(("wire", "valid"), [("9876543210", True), ("5876543210", False),
                                             ("987654321", False), ("+919876543210", False)])
def test_phone_field_accepts_indian_mobile_numbers_only(wire, valid):
    field = HotelPhoneField()

    if valid:
        assert field.run_validation(wire) == wire
    else:
        with pytest.raises(exceptions.ValidationError):
            field.run_validation(wire)


class SearchQuerySerializer(BaseHotelSerializer):
    amenities = HotelCsvListField(child=HotelCharField(max_length=40), required=False)
    stay_types = HotelCsvListField(child=HotelEnumField(StayType), required=False)


@pytest.mark.parametrize(
    ("query", "amenities"),
    [
        ("amenities=WIFI,POOL", ["WIFI", "POOL"]),
        ("amenities=WIFI&amenities=POOL,%20GYM", ["WIFI", "POOL", "GYM"]),
        ("amenities=WIFI,,POOL,", ["WIFI", "POOL"]),
    ],
)
def test_csv_list_field_reads_comma_separated_query_values(query, amenities):
    serializer = SearchQuerySerializer(data=QueryDict(query), context=as_caller(ANONYMOUS))

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["amenities"] == amenities


def test_csv_list_field_converts_each_item_with_its_child():
    valid = SearchQuerySerializer(data={"stay_types": "PREPAID,PAY_AT_PROPERTY"}, context=as_caller(ANONYMOUS))
    invalid = SearchQuerySerializer(data=QueryDict("stay_types=PREPAID,LATER"), context=as_caller(ANONYMOUS))

    assert valid.is_valid(), valid.errors
    assert valid.validated_data["stay_types"] == [StayType.PREPAID, StayType.PAY_AT_PROPERTY]
    assert not invalid.is_valid()
    assert "stay_types" in invalid.errors


def test_encrypted_char_field_requires_mask_as():
    with pytest.raises(TypeError):
        HotelEncryptedCharField()


def test_access_rules_are_added_to_the_help_text():
    field = HotelCharField(help_text="Booking for another user.", restrict_roles=[Role.ADMIN])

    assert field.help_text.startswith("Booking for another user.")
    assert "ADMIN" in field.help_text


def test_unknown_role_is_refused_when_the_field_is_declared():
    with pytest.raises(ValueError):
        HotelCharField(restrict_roles=["SUPERUSER"])


# --- Field-level security: who may send a field -------------------------------------------------------------------


class BookingCreateSerializer(BaseHotelSerializer):
    guest_name = HotelEncryptedCharField(min_length=2, max_length=100, mask_as=PiType.NAME)
    guests = HotelIntegerField(min_value=1, max_value=40)
    on_behalf_of_user_id = HotelCharField(required=False, max_length=64, restrict_roles=[Role.ADMIN])


def test_guest_sending_an_admin_only_field_is_forbidden():
    data = {"guest_name": "Rahul Sharma", "guests": 2, "on_behalf_of_user_id": "guest-9"}

    with pytest.raises(Forbidden) as error:
        BookingCreateSerializer(data=data, context=as_caller(GUEST)).is_valid()

    assert error.value.status_code is codes.FIELD_ACCESS_DENIED
    assert error.value.params == {"field": "on_behalf_of_user_id"}
    assert error.value.http_status == 403


def test_admin_may_send_an_admin_only_field():
    data = {"guest_name": "Rahul Sharma", "guests": 2, "on_behalf_of_user_id": "guest-9"}
    serializer = BookingCreateSerializer(data=data, context=as_caller(ADMIN))

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["on_behalf_of_user_id"] == "guest-9"


def test_anyone_may_leave_an_admin_only_field_out():
    serializer = BookingCreateSerializer(data={"guest_name": "Rahul Sharma", "guests": 2}, context=as_caller(GUEST))

    assert serializer.is_valid(), serializer.errors


def test_forbidden_field_is_reported_before_validation_errors():
    data = {"guest_name": "R", "guests": 0, "on_behalf_of_user_id": "x" * 500}

    with pytest.raises(Forbidden):
        BookingCreateSerializer(data=data, context=as_caller(GUEST)).is_valid()


def test_admin_only_query_parameter_is_forbidden_to_a_guest():
    class BookingListQuerySerializer(BaseHotelSerializer):
        property_id = HotelUUIDField(required=False)
        guest_id = HotelCharField(required=False, restrict_roles=[Role.ADMIN])

    with pytest.raises(Forbidden) as error:
        BookingListQuerySerializer(data=QueryDict("guest_id=guest-2"), context=as_caller(GUEST)).is_valid()
    assert error.value.params == {"field": "guest_id"}


class AddressSerializer(BaseHotelSerializer):
    city = HotelCharField()
    geo_code = HotelCharField(required=False, restrict_roles=[Role.ADMIN])


class RoomTypeSerializer(BaseHotelSerializer):
    name = HotelCharField()
    rack_rate = HotelAmountField(required=False, restrict_roles=[Role.ADMIN])


class PropertySerializer(BaseHotelSerializer):
    name = HotelCharField()
    address = AddressSerializer()
    room_types = RoomTypeSerializer(many=True)


@pytest.mark.parametrize(
    ("address", "room_types", "dotted_name"),
    [
        ({"city": "Goa", "geo_code": "15.5,73.8"}, [{"name": "Deluxe"}], "address.geo_code"),
        ({"city": "Goa"}, [{"name": "Deluxe"}, {"name": "Suite", "rack_rate": "9000"}], "room_types.rack_rate"),
    ],
    ids=["nested-serializer", "list-child"],
)
def test_nested_restricted_fields_are_reported_by_dotted_name(address, room_types, dotted_name):
    data = {"name": "Sea Breeze", "address": address, "room_types": room_types}

    with pytest.raises(Forbidden) as error:
        PropertySerializer(data=data, context=as_caller(OWNER)).is_valid()
    assert error.value.params == {"field": dotted_name}


def test_admin_may_send_nested_restricted_fields():
    data = {
        "name": "Sea Breeze",
        "address": {"city": "Goa", "geo_code": "15.5,73.8"},
        "room_types": [{"name": "Suite", "rack_rate": "9000"}],
    }
    serializer = PropertySerializer(data=data, context=as_caller(ADMIN))

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["room_types"][0]["rack_rate"] == 900_000


def test_caller_falls_back_to_the_request_context():
    data = {"guest_name": "Rahul Sharma", "guests": 2, "on_behalf_of_user_id": "guest-9"}
    tokens = RequestContext.bind(principal=ADMIN)
    try:
        assert BookingCreateSerializer(data=data).is_valid()
    finally:
        RequestContext.reset(tokens)

    with pytest.raises(Forbidden):
        BookingCreateSerializer(data=data).is_valid()  # outside a request the caller is ANONYMOUS


# --- Field-level security: who may see a field --------------------------------------------------------------------


@dataclass(frozen=True)
class Booking:
    booking_id: str
    guest_id: str
    guest_email: str
    internal_note: str


class BookingSerializer(BaseHotelSerializer):
    booking_id = HotelCharField()
    guest_email = HotelEncryptedCharField(read_roles=[Role.ADMIN], subject_attr="guest_id", mask_as=PiType.EMAIL)
    internal_note = HotelCharField(read_roles=[Role.ADMIN])


RAHULS_BOOKING = Booking("b-1", "guest-1", "rahul@example.com", "late arrival")


def test_owner_sees_a_masked_email_and_no_admin_only_field():
    data = BookingSerializer(RAHULS_BOOKING, context=as_caller(OWNER)).data

    assert data == {"booking_id": "b-1", "guest_email": "r****@example.com"}


@pytest.mark.parametrize("instance", [RAHULS_BOOKING, vars(RAHULS_BOOKING)], ids=["object", "mapping"])
def test_subject_sees_own_email_in_plaintext(instance):
    data = BookingSerializer(instance, context=as_caller(GUEST)).data

    assert data == {"booking_id": "b-1", "guest_email": "rahul@example.com"}


def test_admin_sees_every_field_in_plaintext():
    data = BookingSerializer(RAHULS_BOOKING, context=as_caller(ADMIN)).data

    assert data == {"booking_id": "b-1", "guest_email": "rahul@example.com", "internal_note": "late arrival"}


def test_another_guest_sees_the_mask():
    assert BookingSerializer(RAHULS_BOOKING, context=as_caller(OTHER_GUEST)).data["guest_email"] == "r****@example.com"


def test_anonymous_caller_is_never_the_subject():
    anonymous_booking = Booking("b-2", ANONYMOUS.user_id, "rahul@example.com", "")

    data = BookingSerializer(anonymous_booking, context=as_caller(ANONYMOUS)).data

    assert data["guest_email"] == "r****@example.com"


def test_read_rules_apply_to_each_item_of_a_list():
    bookings = [RAHULS_BOOKING, Booking("b-2", "guest-2", "priya@example.com", "")]

    data = BookingSerializer(bookings, many=True, context=as_caller(GUEST)).data

    assert [item["guest_email"] for item in data] == ["rahul@example.com", "p****@example.com"]


def test_read_rules_apply_inside_nested_serializers():
    class BookingPageSerializer(BaseHotelSerializer):
        items = BookingSerializer(many=True)
        total = HotelIntegerField()

    page = {"items": [RAHULS_BOOKING], "total": 1}

    data = BookingPageSerializer(page, context=as_caller(OWNER)).data

    assert data["items"] == [{"booking_id": "b-1", "guest_email": "r****@example.com"}]


# --- Column converters: EnumField stores SMALLINT -----------------------------------------------------------------


def test_enum_column_stores_the_member_value():
    column = EnumField(StayType)

    assert column.get_prep_value(StayType.PAY_AT_PROPERTY) == 2
    assert column.get_prep_value(2) == 2
    assert column.get_prep_value(None) is None


def test_enum_column_loads_members():
    column = EnumField(StayType)

    assert column.from_db_value(2, None, None) is StayType.PAY_AT_PROPERTY
    assert column.from_db_value(None, None, None) is None
    assert column.to_python("1") is StayType.PREPAID


def test_enum_column_rejects_unknown_values_and_foreign_members():
    column = EnumField(StayType)

    with pytest.raises(ValueError):
        column.get_prep_value(7)
    with pytest.raises(TypeError):
        column.get_prep_value(Colour.RED)
    with pytest.raises(DjangoValidationError):
        column.to_python(7)


def test_enum_column_clean_validates_through_the_enum():
    assert EnumField(StayType).clean(2, None) is StayType.PAY_AT_PROPERTY


def test_enum_column_comment_lists_the_members():
    assert EnumField(StayType).db_comment == "PREPAID=1, PAY_AT_PROPERTY=2"
    assert EnumField(StayType, db_comment="How the stay is paid").db_comment == "How the stay is paid"


def test_enum_column_deconstruct_keeps_the_enum():
    _, _, args, kwargs = EnumField(StayType, default=StayType.PREPAID).deconstruct()

    assert kwargs["enum"] is StayType
    assert EnumField(*args, **kwargs).enum is StayType


def test_wire_values_round_trip_through_their_columns():
    stay_type, column = HotelEnumField(StayType), EnumField(StayType)
    stored_type = column.get_prep_value(stay_type.to_internal_value("PAY_AT_PROPERTY"))
    amount = HotelAmountField()
    stored_paisa = amount.to_internal_value("4800.50")

    assert (stored_type, stored_paisa) == (2, 480050)  # SMALLINT and BIGINT paisa
    assert stay_type.to_representation(column.from_db_value(stored_type, None, None)) == "PAY_AT_PROPERTY"
    assert amount.to_representation(stored_paisa) == "4800.50"


# --- BaseHotelModelSerializer builds Hotel*Fields ---------------------------------------------------------------


class Reservation(models.Model):
    id = models.UUIDField(primary_key=True)
    stay_type = EnumField(StayType)
    guest_email = EncryptedTextField(pi_type=PiType.EMAIL)
    nights = models.PositiveSmallIntegerField()

    class Meta:
        app_label = "hotel_commons_tests"
        managed = False


class ReservationSerializer(BaseHotelModelSerializer):
    class Meta:
        model = Reservation
        fields = ["id", "stay_type", "guest_email", "nights"]
        extra_kwargs = {"guest_email": {"read_roles": [Role.ADMIN]}}


def test_model_serializer_builds_hotel_fields_for_its_columns():
    fields = ReservationSerializer().fields

    assert isinstance(fields["id"], HotelUUIDField)
    assert isinstance(fields["nights"], HotelIntegerField)
    assert isinstance(fields["stay_type"], HotelEnumField) and fields["stay_type"].enum is StayType
    assert isinstance(fields["guest_email"], HotelEncryptedCharField) and fields["guest_email"].mask_as is PiType.EMAIL


def test_model_serializer_speaks_member_names_and_masks_pii():
    reservation = Reservation(id=uuid.UUID(int=1), stay_type=StayType.PAY_AT_PROPERTY,
                              guest_email="rahul@example.com", nights=2)

    data = ReservationSerializer(reservation, context=as_caller(GUEST)).data

    assert (data["stay_type"], data["guest_email"], data["nights"]) == ("PAY_AT_PROPERTY", "r****@example.com", 2)


# --- Envelopes ------------------------------------------------------------------------------------------------


@pytest.fixture
def request_id():
    tokens = RequestContext.bind(request_id="req-1")
    yield "req-1"
    RequestContext.reset(tokens)


def test_api_response_wraps_data_in_the_envelope(request_id):
    response = api_response({"booking_id": "b-1"}, http_status=201, headers={"Location": "/hotel/v1/bookings/b-1"})

    assert response.status_code == 201
    assert response.data == {"status": 2100000, "message": "Success", "data": {"booking_id": "b-1"},
                             "request_id": "req-1"}
    assert response["Location"] == "/hotel/v1/bookings/b-1"


def test_validation_errors_become_a_400_envelope_naming_the_field(request_id):
    serializer = BookingCreateSerializer(data={"guest_name": "Rahul Sharma", "guests": "two"}, context=as_caller(GUEST))
    with pytest.raises(exceptions.ValidationError) as error:
        serializer.is_valid(raise_exception=True)

    response = envelope_exception_handler(error.value, {})

    assert response.status_code == 400
    assert (response.data["status"], response.data["message"]) == (2100001, "Invalid request details")
    assert set(response.data["errors"]) == {"guests"}
    assert (response.data["data"], response.data["request_id"]) == (None, "req-1")


def test_app_errors_keep_their_status_code_and_message(request_id):
    response = envelope_exception_handler(Forbidden(codes.FIELD_ACCESS_DENIED, field="on_behalf_of_user_id"), {})

    assert response.status_code == 403
    assert response.data == {"status": 2100005, "message": "Field 'on_behalf_of_user_id' cannot be set by your role",
                             "errors": None, "data": None, "request_id": "req-1"}


def test_too_many_requests_tells_the_client_when_to_retry(request_id):
    response = envelope_exception_handler(TooManyRequests(codes.RATE_LIMITED, retry_after_seconds=12.2), {})

    assert (response.status_code, response["Retry-After"]) == (429, "13")


@pytest.mark.parametrize(
    ("exc", "http_status", "status"),
    [
        (exceptions.NotAuthenticated(), 401, codes.UNAUTHENTICATED),
        (exceptions.AuthenticationFailed(), 401, codes.UNAUTHENTICATED),
        (exceptions.PermissionDenied(), 403, codes.FORBIDDEN),
        (Http404(), 404, codes.NOT_FOUND),
        (exceptions.NotFound(), 404, codes.NOT_FOUND),
        (exceptions.ParseError(), 400, codes.VALIDATION_FAILED),
        (exceptions.UnsupportedMediaType("text/plain"), 415, codes.VALIDATION_FAILED),
    ],
)
def test_framework_errors_map_to_common_status_codes(request_id, exc, http_status, status):
    response = envelope_exception_handler(exc, {})

    assert (response.status_code, response.data["status"]) == (http_status, status.code)


def test_method_not_allowed_names_the_method(request_id):
    response = envelope_exception_handler(exceptions.MethodNotAllowed("DELETE"),
                                          {"request": SimpleNamespace(method="DELETE")})

    assert (response.status_code, response.data["message"]) == (405, "Method DELETE not allowed")


def test_throttled_requests_get_retry_after(request_id):
    response = envelope_exception_handler(exceptions.Throttled(wait=12.2), {})

    assert (response.status_code, response.data["status"], response["Retry-After"]) == (429, 2100007, "13")


def test_unauthenticated_responses_name_the_auth_scheme(request_id):
    exc = exceptions.NotAuthenticated()
    exc.auth_header = "Bearer"

    assert envelope_exception_handler(exc, {})["WWW-Authenticate"] == "Bearer"


def test_unexpected_errors_are_left_to_the_middleware():
    assert envelope_exception_handler(RuntimeError("boom"), {}) is None


@pytest.mark.parametrize(("page", "page_size"), [(0, 20), (1, 0), (1, 51)])
def test_page_request_refuses_out_of_range_values(page, page_size):
    with pytest.raises(ValidationFailed):
        PageRequest(page=page, page_size=page_size)


def test_page_data_shape():
    assert page_data(["a"], page=2, page_size=1, total=3) == {"items": ["a"], "page": 2, "page_size": 1, "total": 3}


# --- OpenAPI ----------------------------------------------------------------------------------------------------


class QuoteSerializer(BaseHotelSerializer):
    stay_type = HotelEnumField(StayType)
    total = HotelAmountField()


class QuoteView(APIView):
    authentication_classes: list = []
    permission_classes: list = []

    @extend_schema(responses={200: envelope(QuoteSerializer, "QuoteEnvelope"), 400: ERROR_ENVELOPE})
    def get(self, request):
        return api_response()


def _resolve(document: dict, schema: dict) -> dict:
    if "allOf" in schema:
        return _resolve(document, schema["allOf"][0])
    if "$ref" in schema:
        return document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def test_openapi_documents_enum_names_rupee_strings_and_envelopes():
    document = SchemaGenerator(patterns=[path("quotes", QuoteView.as_view())]).get_schema(request=None, public=True)
    schemas = document["components"]["schemas"]

    assert set(schemas["QuoteEnvelope"]["properties"]) == {"status", "message", "data", "request_id"}
    assert set(schemas["ErrorEnvelope"]["properties"]) == {"status", "message", "errors", "data", "request_id"}
    quote = schemas["Quote"]["properties"]
    assert _resolve(document, quote["stay_type"])["enum"] == ["PREPAID", "PAY_AT_PROPERTY"]
    assert quote["total"]["type"] == "string"
