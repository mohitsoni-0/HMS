"""Custom serializer fields (LLD §3.14): every serializer field is a ``Hotel*Field``.

Like the reference ``kuhoo.serializers.fields`` (``KuhooIntegerField(restrict_roles=[...])``, ``EnumField``,
``KuhooAmountField``), each field owns its wire <-> internal conversion and carries its own access rule:

- amounts travel as rupee strings ("4800.50") and live as integer paisa (480050), stored as BIGINT;
- enums travel as member names ("PREPAID") and live as members, stored as SMALLINT by the model ``EnumField``;
- ``restrict_roles`` / ``read_roles`` / ``subject_attr`` / ``mask_as`` are enforced by
  ``hotel_commons.security.field_level.FieldLevelSecurityMixin``.

Invalid input raises DRF's ``ValidationError`` (via ``self.fail``), so a serializer reports every field's
error under its name and the envelope exception handler turns them into 400 VALIDATION_FAILED.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime, time
from datetime import timezone as dt_timezone
from decimal import Context, Decimal
from enum import Enum as StdEnum
from typing import Any, Final

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema_field
from rest_framework import ISO_8601, serializers

from hotel_commons.domain.money import MAX_PAISA, Money
from hotel_commons.enum import Enum
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.principal import Role

_INTEGER: Final = re.compile(r"[+-]?[0-9]+")
_ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HH_MM: Final = re.compile(r"[0-9]{2}:[0-9]{2}")
_INDIAN_MOBILE: Final = re.compile(r"[6-9][0-9]{9}")
_RUPEES_TEXT: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_MAX_RUPEES_TEXT_LENGTH: Final = 32
_MAX_EMAIL_LENGTH: Final = 254  # RFC 5321
_DECIMALS: Final = Context(prec=40)  # wide enough for any BIGINT paisa amount, so no step ever rounds
_MAX_RUPEES: Final = Decimal(MAX_PAISA).scaleb(-2, context=_DECIMALS)


class HotelFieldMixin:
    """Common kwargs for every Hotel*Field:

    - ``restrict_roles``: roles allowed to SEND the field (empty = anyone); anyone else gets 403.
    - ``read_roles``: roles allowed to SEE the raw value (empty = everyone).
    - ``subject_attr``: attribute of the serialised object naming the data subject, who may always see their
      own value (e.g. ``"guest_id"``).
    - ``mask_as``: when the value is not readable, mask it as this PiType; without it the key is dropped.

    The help text is extended with the access rule, as kuhoo's ``help_text_for_access_role`` does, so the
    OpenAPI description shows it.
    """

    def __init__(
        self,
        *args: Any,
        restrict_roles: Iterable[Role | str] = (),
        read_roles: Iterable[Role | str] = (),
        subject_attr: str | None = None,
        mask_as: PiType | None = None,
        **kwargs: Any,
    ) -> None:
        if mask_as is not None and not isinstance(mask_as, PiType):
            raise TypeError("mask_as must be a PiType member")
        self.restrict_roles: frozenset[Role] = frozenset(Role(role) for role in restrict_roles)
        self.read_roles: frozenset[Role] = frozenset(Role(role) for role in read_roles)
        self.subject_attr = subject_attr
        self.mask_as = mask_as
        super().__init__(*args, **kwargs)
        self.help_text = _with_access_rule(self.help_text, self)


class HotelCharField(HotelFieldMixin, serializers.CharField):
    """Text. Surrounding whitespace is trimmed; ``max_length`` is enforced."""


class HotelIntegerField(HotelFieldMixin, serializers.IntegerField):
    """Whole number from a JSON integer or a digit string (query parameters); never truncates a fraction."""

    def to_internal_value(self, data: Any) -> int:
        if isinstance(data, bool | float | Decimal) or (isinstance(data, str) and not _INTEGER.fullmatch(data.strip())):
            self.fail("invalid")
        return super().to_internal_value(data)


class HotelBooleanField(HotelFieldMixin, serializers.BooleanField):
    """Boolean."""


class HotelUUIDField(HotelFieldMixin, serializers.UUIDField):
    """UUID, rendered in its canonical hyphenated form."""


class HotelDateField(HotelFieldMixin, serializers.DateField):
    """Calendar date as "YYYY-MM-DD" only; ISO-8601 week or basic forms are refused."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("format", ISO_8601)
        kwargs.setdefault("input_formats", [ISO_8601])
        super().__init__(**kwargs)

    def to_internal_value(self, value: Any) -> date:
        if isinstance(value, str) and not _ISO_DATE.fullmatch(value):
            self.fail("invalid", format="YYYY-MM-DD")
        return super().to_internal_value(value)


class HotelDateTimeField(HotelFieldMixin, serializers.DateTimeField):
    """Instant as ISO-8601 with an explicit UTC offset; always rendered in UTC ("2026-11-20T08:30:00Z")."""

    default_error_messages = {"offset_required": "Datetime must include a UTC offset, e.g. 2026-11-20T14:00:00+05:30."}

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("format", ISO_8601)
        kwargs.setdefault("input_formats", [ISO_8601])
        kwargs.setdefault("default_timezone", dt_timezone.utc)
        super().__init__(**kwargs)

    def to_internal_value(self, value: Any) -> datetime:
        if _is_naive_datetime(value):
            self.fail("offset_required")
        return super().to_internal_value(value)


class HotelTimeField(HotelFieldMixin, serializers.TimeField):
    """Wall-clock time as "HH:MM"."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("format", "%H:%M")
        kwargs.setdefault("input_formats", ["%H:%M"])
        super().__init__(**kwargs)

    def to_internal_value(self, value: Any) -> time:
        if isinstance(value, str) and not _HH_MM.fullmatch(value):
            self.fail("invalid", format="hh:mm")
        return super().to_internal_value(value)


class HotelEmailField(HotelFieldMixin, serializers.EmailField):
    """E-mail address, lower-cased."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", _MAX_EMAIL_LENGTH)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        return super().to_internal_value(data).lower()


class HotelPhoneField(HotelCharField):
    """Indian mobile number: ten digits starting with 6, 7, 8 or 9 (^[6-9]\\d{9}$)."""

    default_error_messages = {"invalid_phone": "Enter a 10-digit mobile number starting with 6, 7, 8 or 9."}

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        if not _INDIAN_MOBILE.fullmatch(value):
            self.fail("invalid_phone")
        return value


class HotelListField(HotelFieldMixin, serializers.ListField):
    """List whose items are converted by ``child`` (itself a Hotel*Field)."""


class HotelCsvListField(HotelListField):
    """List read from comma-separated text: ``?amenities=WIFI,POOL`` -> ["WIFI", "POOL"].

    Repeated query parameters and JSON lists are accepted too; blank items are ignored.
    """

    def to_internal_value(self, data: Any) -> list[Any]:
        if isinstance(data, str):
            data = [data]
        if isinstance(data, list):
            data = [item for raw in data for item in _split_csv(raw)]
        return super().to_internal_value(data)


class HotelEnumField(HotelFieldMixin, serializers.Field):
    """Enum converter. DB: SMALLINT via the model ``EnumField``. Wire: the member name.

    ``label_name`` picks another attribute for the wire (e.g. ``"label"``). Input must be one of those
    names; the stored integer is refused, so it never becomes part of the API contract.
    """

    default_error_messages = {"invalid_choice": "Must be one of {choices}."}

    def __init__(self, enum: type[Enum], *, label_name: str = "name", **kwargs: Any) -> None:
        self.enum = enum
        self.label_name = label_name
        self._members: dict[str, Enum] = {getattr(member, label_name): member for member in enum}
        self._choices = ", ".join(self._members)
        kwargs["help_text"] = " ".join(filter(None, [kwargs.get("help_text"), f"One of: {self._choices}."]))
        super().__init__(**kwargs)
        extend_schema_field({"type": "string", "enum": list(self._members)})(self)

    def to_internal_value(self, data: Any) -> Enum:
        member = self._members.get(data) if isinstance(data, str) else None
        if member is None:
            self.fail("invalid_choice", choices=self._choices)
        return member

    def to_representation(self, value: Enum | int) -> str:
        return getattr(self._member(value), self.label_name)

    def _member(self, value: Enum | int) -> Enum:
        if isinstance(value, self.enum):
            return value
        if isinstance(value, StdEnum | bool) or not isinstance(value, int):
            raise TypeError(f"{value!r} is neither a {self.enum.__name__} member nor its stored int")
        return self.enum(value)


@extend_schema_field(
    {"type": "string", "format": "decimal", "pattern": r"^\d+(\.\d{1,2})?$", "example": "4800.50"}
)
class HotelAmountField(HotelFieldMixin, serializers.Field):
    """Money converter. DB and domain: integer paisa. Wire: rupees as a 2-dp string.

    Input is rupees as a string, int or Decimal ("4800.5", 4800, Decimal("4800.50")); a float is read
    through ``str()``, never its binary value, so 0.1 + 0.2 is refused instead of rounded. Output is
    "4800.50" for 480050 or ``Money(480050)``.
    """

    default_error_messages = {
        "invalid": 'Enter an amount in rupees, e.g. "4800.50".',
        "negative": "Ensure the amount is not negative.",
        "max_decimal_places": "Ensure the amount has no more than 2 decimal places.",
        "min_value": "Ensure the amount is at least {limit}.",
        "max_value": "Ensure the amount is at most {limit}.",
    }

    def __init__(self, *, min_rupees: Decimal | None = None, max_rupees: Decimal | None = None, **kwargs: Any) -> None:
        self.min_rupees = None if min_rupees is None else Decimal(str(min_rupees))
        self.max_rupees = _MAX_RUPEES if max_rupees is None else min(Decimal(str(max_rupees)), _MAX_RUPEES)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> int:
        rupees = self._parse(data)
        if rupees < 0:
            self.fail("negative")
        if self.min_rupees is not None and rupees < self.min_rupees:
            self.fail("min_value", limit=_format_rupees(self.min_rupees))
        if rupees > self.max_rupees:
            self.fail("max_value", limit=_format_rupees(self.max_rupees))
        if _has_sub_paisa(rupees):
            self.fail("max_decimal_places")
        return int(rupees.scaleb(2, context=_DECIMALS))

    def to_representation(self, value: int | Money) -> str:
        paisa = value.paisa if isinstance(value, Money) else value
        if isinstance(paisa, bool) or not isinstance(paisa, int):
            raise TypeError(f"HotelAmountField renders integer paisa or Money, not {type(value).__name__}")
        return format(Decimal(paisa).scaleb(-2, context=_DECIMALS), "f")

    def _parse(self, data: Any) -> Decimal:
        if isinstance(data, str):
            text = data.strip()
            if len(text) > _MAX_RUPEES_TEXT_LENGTH or not _RUPEES_TEXT.fullmatch(text):
                self.fail("invalid")
            return Decimal(text)
        if isinstance(data, bool) or not isinstance(data, int | float | Decimal):
            self.fail("invalid")
        rupees = Decimal(str(data)) if isinstance(data, float) else Decimal(data)
        if not rupees.is_finite():
            self.fail("invalid")
        return rupees


class HotelEncryptedCharField(HotelCharField):
    """PII text. ``mask_as`` is required; the value is masked on output unless the read rule passes.

    The serializer only ever handles plaintext (encryption at rest is the model ``EncryptedTextField``'s job),
    and the value is never logged or echoed in an error message.
    """

    def __init__(self, *, mask_as: PiType, **kwargs: Any) -> None:
        super().__init__(mask_as=mask_as, **kwargs)


def _with_access_rule(help_text: Any, field: HotelFieldMixin) -> str | None:
    notes = []
    if field.restrict_roles:
        notes.append(f"Only {_role_names(field.restrict_roles)} may send this field.")
    if field.read_roles:
        readers = _role_names(field.read_roles) + (" and the data subject" if field.subject_attr else "")
        hidden = f"masked as {field.mask_as.name}" if field.mask_as else "omitted"
        notes.append(f"Visible to {readers}; {hidden} for everyone else.")
    return " ".join([str(help_text), *notes]) if help_text else " ".join(notes) or None


def _role_names(roles: frozenset[Role]) -> str:
    return ", ".join(sorted(role.value for role in roles))


def _is_naive_datetime(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value = parse_datetime(value)
        except ValueError:
            return False  # well formed but impossible: the parent field reports it
    return isinstance(value, datetime) and timezone.is_naive(value)


def _split_csv(raw: Any) -> list[Any]:
    if not isinstance(raw, str):
        return [raw]
    return [part.strip() for part in raw.split(",") if part.strip()]


def _has_sub_paisa(rupees: Decimal) -> bool:
    """True when a non-zero digit sits beyond the second decimal place; exact, so "4800.500" is fine."""
    _, digits, exponent = rupees.as_tuple()
    extra = -2 - int(exponent)
    return extra > 0 and any(digits[-extra:])


def _format_rupees(rupees: Decimal) -> str:
    return format(rupees.quantize(Decimal("0.01"), context=_DECIMALS), "f")
