"""Integer enums for the whole service: SMALLINT in the database, member names on the wire.

The model ``EnumField`` stores ``int(member)`` and the serializer ``HotelEnumField`` sends ``member.name``, so
the integer never leaks into the API and labels can change freely. Every subclass is checked when it is
created: values must be unique plain ints that fit a PostgreSQL SMALLINT column (0..32767), otherwise the
module defining it fails to import instead of failing at the first database write.
"""

import enum
from collections.abc import Mapping
from typing import Any, Final, Self

SMALLINT_MAX: Final = 32767


class Enum(enum.Enum):
    """Base enum with int conversion and display labels (kuhoo's ``Enum`` and ``LabelEnum`` in one).

    ``label`` is the title-cased name ("PAY_AT_PROPERTY" -> "Pay At Property") unless the subclass declares
    ``__labels__ = {value: "Display text"}``.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _require_unique_smallint_values(cls)

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)

    @property
    def label(self) -> str:
        labels: Mapping[int, str] = getattr(type(self), "__labels__", {})
        return labels.get(self.value) or self.name.replace("_", " ").title()

    @classmethod
    def from_label(cls, label: str) -> Self:
        for member in cls:
            if member.label == label:
                return member
        raise ValueError(f"{label!r} is not a {cls.__name__} label")


def _require_unique_smallint_values(cls: type[enum.Enum]) -> None:
    for name, member in cls.__members__.items():
        value = member.value
        if type(value) is not int:  # bool and other int subclasses would not round-trip through the column
            raise TypeError(f"{cls.__name__}.{name} needs a plain int value (stored as SMALLINT), got {value!r}")
        if not 0 <= value <= SMALLINT_MAX:
            raise ValueError(f"{cls.__name__}.{name} = {value} does not fit SMALLINT (0..{SMALLINT_MAX})")
    aliases = [name for name, member in cls.__members__.items() if member.name != name]
    if aliases:
        raise ValueError(f"{cls.__name__} reuses values for {', '.join(aliases)}; each member needs its own value")
