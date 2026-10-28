"""IDOR coverage of the HTTP API (LLD §3.7), checked on the live URLconf.

Rules, over every route served by a ``BaseViewSet`` action:
- each ``*_id`` path kwarg is guarded by one of the action's ``IdorRule``s (source "path", same key), unless the
  action is listed in ``PUBLIC_ACTIONS`` with the reason it may be read by anyone;
- every ``PUBLIC_ACTIONS`` entry is still routed and still ``AllowAny``, so an exemption cannot outlive its reason;
- every resource an ``IdorRule`` names has an ownership resolver, so a rule never fails with a 500 at runtime.

A new endpoint that takes an id therefore fails here until its author decides who may reach that id.
Only the URLconf and the app registry are loaded; no database is needed.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import AllowAny

from hotel_commons.api.views import BaseViewSet
from hotel_commons.security.idor import IdorRule, ownership_registry

# (viewset, action) -> why anyone may reach the ids in its path.
PUBLIC_ACTIONS = {
    ("PropertyViewSet", "retrieve"): "a property page is public catalog data, like search results",
}


@dataclass(frozen=True, slots=True)
class Route:
    path: str
    view: type[BaseViewSet]
    action: str
    id_kwargs: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.view.__name__}.{self.action} ({self.path})"


def routes_of(patterns: Sequence[Any], prefix: str = "", kwargs: tuple[str, ...] = ()) -> Iterator[Route]:
    """Every (viewset, action) the URLconf serves, with the path kwargs collected along its includes."""
    for pattern in patterns:
        path = prefix + str(pattern.pattern)
        names = kwargs + tuple(getattr(pattern.pattern, "converters", {}))
        if isinstance(pattern, URLResolver):
            yield from routes_of(pattern.url_patterns, path, names)
            continue
        view = getattr(pattern.callback, "cls", None)
        if not (isinstance(pattern, URLPattern) and isinstance(view, type) and issubclass(view, BaseViewSet)):
            continue
        id_kwargs = tuple(name for name in names if name.endswith("_id"))
        for action in dict.fromkeys(getattr(pattern.callback, "actions", {}).values()):
            yield Route(path, view, action, id_kwargs)


def unguarded_ids(route: Route) -> list[str]:
    """The id kwargs of ``route`` no path rule of its action covers; nothing for a ``PUBLIC_ACTIONS`` entry."""
    if (route.view.__name__, route.action) in PUBLIC_ACTIONS:
        return []
    guarded = {rule.key for rule in route.view.idor_rules.get(route.action, ()) if rule.source == "path"}
    return [f"{route}: {kwarg}" for kwarg in route.id_kwargs if kwarg not in guarded]


@pytest.fixture(scope="module")
def routes() -> list[Route]:
    return list(routes_of(get_resolver().url_patterns))


def test_every_id_in_a_path_is_guarded_by_an_idor_rule(routes: list[Route]) -> None:
    violations = [violation for route in routes for violation in unguarded_ids(route)]

    assert violations == [], "add an IdorRule for the id, or a PUBLIC_ACTIONS entry saying why anyone may read it"


def test_public_exemptions_are_still_routed_and_anonymous(routes: list[Route]) -> None:
    served = {(route.view.__name__, route.action): route.view for route in routes}

    for key in PUBLIC_ACTIONS:
        assert key in served, f"{key} is no longer routed: drop it from PUBLIC_ACTIONS"
        view, action = served[key], key[1]
        assert AllowAny in view.permission_classes_by_action.get(action, view.permission_classes), (
            f"{key} now requires authentication: give it an IdorRule and drop it from PUBLIC_ACTIONS"
        )


def test_every_resource_a_rule_names_has_a_resolver(routes: list[Route]) -> None:
    resources = {rule.resource for route in routes for rule in route.view.idor_rules.get(route.action, ())}

    assert resources, "the URLconf exposes no IDOR rule: the walk over it is broken"
    assert sorted(resource for resource in resources if resource not in ownership_registry) == []


# --- The check itself (negative controls on synthetic viewsets) ----------------------------------------------


class _Unguarded(BaseViewSet):
    pass


class _Guarded(BaseViewSet):
    idor_rules = {"retrieve": (IdorRule("booking", "booking_id"),)}


class _GuardedByBody(BaseViewSet):
    idor_rules = {"retrieve": (IdorRule("booking", "booking_id", source="body"),)}


def test_checker_flags_an_id_without_a_rule() -> None:
    route = Route("bookings/<uuid:booking_id>", _Unguarded, "retrieve", ("booking_id",))

    assert unguarded_ids(route) == ["_Unguarded.retrieve (bookings/<uuid:booking_id>): booking_id"]


def test_checker_flags_a_rule_reading_the_id_from_elsewhere() -> None:
    route = Route("bookings/<uuid:booking_id>", _GuardedByBody, "retrieve", ("booking_id",))

    assert unguarded_ids(route) != []


def test_checker_accepts_a_path_rule_on_the_same_key() -> None:
    route = Route("bookings/<uuid:booking_id>", _Guarded, "retrieve", ("booking_id",))

    assert unguarded_ids(route) == []


def test_walk_finds_the_known_guarded_routes(routes: list[Route]) -> None:
    found = {(route.view.__name__, route.action, route.id_kwargs) for route in routes}

    assert ("BookingViewSet", "cancel", ("booking_id",)) in found
    assert ("PaymentViewSet", "retrieve", ("payment_id",)) in found
    assert ("PropertyViewSet", "onboard", ("account_id",)) in found
