"""Object-level authorisation (LLD §3.7): may this caller touch the resource named in the request?

Each app registers an ``OwnershipResolver`` per resource in its ``AppConfig.ready()``; views declare
``IdorRule``s per action. The guard reads every id a rule names, asks the resolver who the resource
belongs to, and accepts the caller only through a relation the rule allows: a GUEST through
``guest_user_ids``, an OWNER through ``owner_user_ids``. A token holding both roles therefore cannot reach
a stranger's booking as its guest merely because it owns the property. A malformed, unknown or foreign id
all answer the same 404, so the API never reveals that a resource exists. ADMIN bypasses the check.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from hotel_commons.exceptions import NotFound
from hotel_commons.security.principal import Principal, Role
from hotel_commons.status_code import RESOURCE_NOT_FOUND

logger = logging.getLogger(__name__)

Relation = Literal["guest", "owner"]
Source = Literal["path", "body", "query"]


@dataclass(frozen=True, slots=True)
class Ownership:
    guest_user_ids: frozenset[str] = frozenset()
    owner_user_ids: frozenset[str] = frozenset()


class OwnershipResolver(ABC):
    @abstractmethod
    def resolve(self, resource_id: UUID) -> Ownership | None:
        """Who the resource belongs to; None when there is no such (active) resource."""


class OwnershipRegistry:
    """Resource name -> resolver. Reads go straight to the resolver (no cache), so revocation is immediate."""

    def __init__(self) -> None:
        self._resolvers: dict[str, OwnershipResolver] = {}

    def register(self, resource: str, resolver: OwnershipResolver) -> None:
        """Register ``resolver``; re-registering the same kind replaces it (``ready()`` may run twice in tests),
        a different kind for a taken name is a ValueError (two apps claiming one resource)."""
        current = self._resolvers.get(resource)
        if current is not None and type(current) is not type(resolver):
            raise ValueError(f"Resource {resource!r} already has a {type(current).__name__} resolver")
        self._resolvers[resource] = resolver

    def resolve(self, resource: str, resource_id: UUID) -> Ownership | None:
        """The ownership of ``resource_id``; LookupError for an unregistered resource (a wiring bug, so a 500)."""
        resolver = self._resolvers.get(resource)
        if resolver is None:
            raise LookupError(f"No ownership resolver is registered for {resource!r}")
        return resolver.resolve(resource_id)


ownership_registry = OwnershipRegistry()


@dataclass(frozen=True, slots=True)
class IdorRule:
    resource: str  # "booking", "property", "owner_account", "payment"
    key: str  # path kwarg / body field / query param
    source: Source = "path"
    relations: frozenset[Relation] = frozenset({"guest", "owner"})


class IdorGuard:
    def __init__(self, registry: OwnershipRegistry = ownership_registry) -> None:
        self._registry = registry

    def check(
        self,
        principal: Principal,
        rules: Sequence[IdorRule],
        *,
        path_kwargs: Mapping[str, Any],
        body: Mapping[str, Any],
        query: Mapping[str, Any],
    ) -> None:
        """Return when every id named by ``rules`` is accessible, else raise NotFound(RESOURCE_NOT_FOUND)."""
        if principal.has_any_role(Role.ADMIN):
            return
        sources: dict[Source, Mapping[str, Any]] = {"path": path_kwargs, "body": body, "query": query}
        for rule in rules:
            for value in _values(sources[rule.source], rule.key):
                self._check_one(principal, rule, value)

    @staticmethod
    def can_access(principal: Principal, ownership: Ownership, relations: frozenset[Relation]) -> bool:
        user_id = principal.user_id
        as_guest = (
            "guest" in relations and principal.has_any_role(Role.GUEST) and user_id in ownership.guest_user_ids
        )
        as_owner = (
            "owner" in relations and principal.has_any_role(Role.OWNER) and user_id in ownership.owner_user_ids
        )
        return as_guest or as_owner

    def _check_one(self, principal: Principal, rule: IdorRule, value: Any) -> None:
        resource_id = _parse_uuid(value)
        ownership = self._registry.resolve(rule.resource, resource_id) if resource_id is not None else None
        if ownership is None or not self.can_access(principal, ownership, rule.relations):
            logger.info("Access to %s %s denied for %s", rule.resource, resource_id, principal.user_id)
            raise NotFound(RESOURCE_NOT_FOUND, resource=rule.resource)


def _values(source: Mapping[str, Any], key: str) -> Iterator[Any]:
    """Every value sent under ``key``: all of a repeated query parameter, each element of a list, or the one
    value. An absent or null value yields nothing; the serializer decides whether it was required."""
    if key not in source:
        return
    raw = source.getlist(key) if hasattr(source, "getlist") else source[key]
    for value in raw if isinstance(raw, list | tuple) else [raw]:
        if value is not None:
            yield value


def _parse_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
