"""Model fields that carry the storage conventions (LLD §2.11, §3.6).

- ``EnumField``: an ``hotel_commons.enum.Enum`` stored as SMALLINT; Python code only ever sees members.
- ``EncryptedTextField``: PII as AES-256-GCM ciphertext at rest and plaintext in Python. Encryption sits on
  the field, so every write path (save, update, bulk_create, bulk_update, log rows) is covered.
- ``BlindIndexField``: the keyed hash that equality lookups on an encrypted column use instead.
"""

from enum import Enum as StdEnum
from typing import Any, Final

from django.core.exceptions import FieldError, ValidationError
from django.db import models
from django.utils.functional import cached_property

from hotel_commons.enum import Enum
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.pii import Ciphertext, PiiCipher, get_pii_cipher


class EnumField(models.PositiveSmallIntegerField):
    """SMALLINT column holding an Enum's value; reads, writes and lookups take members (or their ints)."""

    def __init__(self, enum: type[Enum], *args: Any, **kwargs: Any) -> None:
        self.enum = enum
        kwargs.setdefault("db_comment", ", ".join(f"{member.name}={member.value}" for member in enum))
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs["enum"] = self.enum
        return name, path, args, kwargs

    @cached_property
    def validators(self) -> list[Any]:
        # Skips IntegerField's range validators: members do not compare with ints, and the enum bounds the values.
        return [*self.default_validators, *self._validators]

    def from_db_value(self, value: int | None, expression: Any, connection: Any) -> Enum | None:
        return None if value is None else self.enum(value)

    def to_python(self, value: Any) -> Enum | None:
        if value is None:
            return None
        try:
            return self._member(value)
        except (TypeError, ValueError):
            raise ValidationError(f"Not a valid {self.enum.__name__}", code="invalid_choice") from None

    def get_prep_value(self, value: Any) -> int | None:
        return None if value is None else self._member(value).value

    def _member(self, value: Any) -> Enum:
        if isinstance(value, self.enum):
            return value
        if isinstance(value, StdEnum | bool):  # int() of these would silently store the wrong member
            raise TypeError(f"{value!r} is not a {self.enum.__name__} member")
        return self.enum(int(value) if isinstance(value, str) else value)


class EncryptedTextField(models.TextField):
    """PII column: AES-256-GCM ciphertext at rest, plaintext in Python.

    The associated data is ``"<PI type>|<column name>"``: identical on the live and log tables (same column
    names), different for every other column. Only ``Ciphertext`` passes through unchanged; any other value,
    including user input that starts with ``ENC_``, is encrypted.
    """

    def __init__(self, *args: Any, pi_type: PiType, **kwargs: Any) -> None:
        self.pi_type = pi_type
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs["pi_type"] = self.pi_type
        return name, path, args, kwargs

    def get_prep_value(self, value: Any) -> str | None:
        if value is None or isinstance(value, Ciphertext):
            return value
        return get_pii_cipher().encrypt(str(value), aad=self._aad)

    def from_db_value(self, value: str | None, expression: Any, connection: Any) -> str | None:
        if not PiiCipher.looks_encrypted(value):
            return value  # NULL, or a row written before the column was encrypted
        return get_pii_cipher().decrypt(value, aad=self._aad)

    def to_python(self, value: Any) -> Any:
        return value  # never decrypts: input that looks like ciphertext is just text

    def get_lookup(self, lookup_name: str) -> Any:
        if lookup_name != "isnull":
            raise FieldError(
                f"{self.name} is encrypted and cannot be filtered with '{lookup_name}'; query {self.name}_hash instead"
            )
        return super().get_lookup(lookup_name)

    @property
    def _aad(self) -> str:
        return f"{self.pi_type.name}|{self.name}"


class BlindIndexField(models.CharField):
    """Hex HMAC-SHA256 of a PII value (``<field>_hash``), filled on every write by ``secure_pii_companions``."""

    _DEFAULTS: Final = {"max_length": 64, "null": True, "db_index": True, "editable": False}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        for name, default in self._DEFAULTS.items():
            kwargs.setdefault(name, default)
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        for option, default in self._DEFAULTS.items():
            current = getattr(self, option)
            if current == default:
                kwargs.pop(option, None)
            else:
                kwargs[option] = current  # explicit, or the default would come back on reconstruction
        return name, path, args, kwargs
