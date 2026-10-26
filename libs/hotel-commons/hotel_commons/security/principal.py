"""The authenticated caller: roles and the ``Principal`` value object (LLD §3.3).

Standard library only, so the domain, the request context and DRF can all share it. A ``Principal`` doubles
as DRF's ``request.user`` (DRF only reads ``is_authenticated``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Role(StrEnum):
    GUEST = "GUEST"
    OWNER = "OWNER"
    ADMIN = "ADMIN"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. ``roles`` is normalised to a frozenset of ``Role``; an unknown role raises ValueError."""

    user_id: str
    roles: frozenset[Role]
    is_authenticated: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(Role(role) for role in self.roles))

    def has_any_role(self, *roles: Role) -> bool:
        """True when the caller is authenticated and holds at least one of ``roles``."""
        return self.is_authenticated and not self.roles.isdisjoint(roles)


ANONYMOUS: Final = Principal(user_id="anonymous", roles=frozenset(), is_authenticated=False)
SYSTEM_ACTOR: Final = "system"
