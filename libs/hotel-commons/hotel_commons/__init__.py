"""hotel_commons: shared kernel and infrastructure for the hotel booking service.

``Service(name="hotel")`` records which service the library runs in, the way kuhoo's ``Service`` does; the
project's settings call it once at start-up. Subpackages are imported explicitly, nothing is re-exported
here, so importing a pure module (``hotel_commons.domain.money``) never loads Django.
"""

from typing import ClassVar

from hotel_commons.consts import SERVICE_CODES


class Service:
    """Process-wide service identity. The name lives on the class, so every module reads the same value."""

    name: ClassVar[str | None] = None

    def __init__(self, name: str) -> None:
        normalised = name.strip().lower()
        if normalised not in SERVICE_CODES:
            known = ", ".join(sorted(SERVICE_CODES))
            raise ValueError(f"Unknown service {name!r}; register it in hotel_commons.consts.SERVICE_CODES ({known})")
        Service.name = normalised
