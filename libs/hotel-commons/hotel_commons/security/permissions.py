"""DRF permissions over the ``Principal`` (LLD §3.7).

``request.user`` is the Principal that ``PrincipalAuthentication`` took from the JWT middleware, or None
for an anonymous call (``UNAUTHENTICATED_USER = None``). A failed check on an anonymous call becomes 401
(DRF raises NotAuthenticated because an authenticator exists); on an authenticated call it becomes 403
FORBIDDEN through the envelope exception handler.
"""

from functools import cache
from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from hotel_commons.security.principal import ANONYMOUS, Principal, Role


def principal_of(request: Request) -> Principal:
    """The authenticated caller of a DRF request, or ``ANONYMOUS``."""
    user = getattr(request, "user", None)
    return user if isinstance(user, Principal) else ANONYMOUS


class IsAuthenticatedPrincipal(BasePermission):
    def has_permission(self, request: Request, view: Any) -> bool:
        return principal_of(request).is_authenticated


def require_roles(*roles: Role) -> type[BasePermission]:
    """A permission class admitting callers holding any of ``roles``; the same set always gives the same class."""
    if not roles:
        raise ValueError("require_roles() needs at least one role")
    return _role_permission(frozenset(Role(role) for role in roles))


@cache
def _role_permission(roles: frozenset[Role]) -> type[BasePermission]:
    names = sorted(role.value for role in roles)

    class RolePermission(BasePermission):
        allowed_roles = roles

        def has_permission(self, request: Request, view: Any) -> bool:
            return principal_of(request).has_any_role(*self.allowed_roles)

    RolePermission.__name__ = RolePermission.__qualname__ = "Requires" + "Or".join(name.title() for name in names)
    return RolePermission
