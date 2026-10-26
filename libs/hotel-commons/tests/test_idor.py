"""IDOR guard (LLD §3.7): ownership resolved per resource, checked per relation, hidden behind 404.

Pure: an in-memory resolver stands in for the apps' ORM resolvers.
"""

from uuid import UUID, uuid4

import pytest
from hotel_commons.exceptions import NotFound
from hotel_commons.security.idor import IdorGuard, IdorRule, Ownership, OwnershipRegistry, OwnershipResolver
from hotel_commons.security.principal import ANONYMOUS, Principal, Role
from hotel_commons.status_code import RESOURCE_NOT_FOUND

GUEST_ID = "guest-1"
OWNER_ID = "owner-1"
BOOKING_ID = uuid4()
OTHER_BOOKING_ID = uuid4()


class MultiValueQuery(dict):
    """The QueryDict surface the guard reads: ``getlist`` returns every value sent for a key."""

    def getlist(self, key: str) -> list[str]:
        return list(self.get(key, []))


class DictResolver(OwnershipResolver):
    def __init__(self, ownerships: dict[UUID, Ownership]) -> None:
        self.ownerships = ownerships
        self.calls: list[UUID] = []

    def resolve(self, resource_id: UUID) -> Ownership | None:
        self.calls.append(resource_id)
        return self.ownerships.get(resource_id)


@pytest.fixture
def resolver() -> DictResolver:
    return DictResolver(
        {
            BOOKING_ID: Ownership(guest_user_ids=frozenset({GUEST_ID}), owner_user_ids=frozenset({OWNER_ID})),
            OTHER_BOOKING_ID: Ownership(guest_user_ids=frozenset({"guest-2"}), owner_user_ids=frozenset({OWNER_ID})),
        }
    )


@pytest.fixture
def guard(resolver) -> IdorGuard:
    registry = OwnershipRegistry()
    registry.register("booking", resolver)
    return IdorGuard(registry)


def _principal(user_id: str, *roles: Role) -> Principal:
    return Principal(user_id=user_id, roles=frozenset(roles))


def _check(guard: IdorGuard, principal: Principal, rule: IdorRule, *, path=None, body=None, query=None) -> None:
    guard.check(principal, [rule], path_kwargs=path or {}, body=body or {}, query=query or {})


def _assert_hidden(guard: IdorGuard, principal: Principal, rule: IdorRule, **sources) -> None:
    with pytest.raises(NotFound) as error:
        _check(guard, principal, rule, **sources)
    assert error.value.status_code == RESOURCE_NOT_FOUND
    assert error.value.message == "booking not found"


GUEST_RULE = IdorRule(resource="booking", key="booking_id", relations=frozenset({"guest"}))
ANY_RULE = IdorRule(resource="booking", key="booking_id")


def test_guest_reaches_own_booking(guard):
    _check(guard, _principal(GUEST_ID, Role.GUEST), GUEST_RULE, path={"booking_id": BOOKING_ID})


def test_other_guest_gets_not_found(guard):
    _assert_hidden(guard, _principal("guest-2", Role.GUEST), GUEST_RULE, path={"booking_id": BOOKING_ID})


def test_dual_role_owner_cannot_use_owner_relation_where_only_guest_applies(guard):
    dual = _principal(OWNER_ID, Role.GUEST, Role.OWNER)

    _assert_hidden(guard, dual, GUEST_RULE, path={"booking_id": OTHER_BOOKING_ID})


def test_owner_relation_admits_the_property_owner(guard):
    _check(guard, _principal(OWNER_ID, Role.OWNER), ANY_RULE, path={"booking_id": str(OTHER_BOOKING_ID)})


def test_relation_needs_the_matching_role(guard):
    # The user id is listed as owner, but the token does not carry the OWNER role.
    _assert_hidden(guard, _principal(OWNER_ID, Role.GUEST), ANY_RULE, path={"booking_id": OTHER_BOOKING_ID})


@pytest.mark.parametrize("value", ["not-a-uuid", "", 42, {"id": "x"}])
def test_malformed_id_is_not_found(guard, resolver, value):
    _assert_hidden(guard, _principal(GUEST_ID, Role.GUEST), GUEST_RULE, path={"booking_id": value})
    assert resolver.calls == []


def test_unknown_id_is_not_found(guard):
    _assert_hidden(guard, _principal(GUEST_ID, Role.GUEST), GUEST_RULE, path={"booking_id": uuid4()})


def test_admin_bypasses_without_resolving(guard, resolver):
    _check(guard, _principal("admin-1", Role.ADMIN), GUEST_RULE, path={"booking_id": "not-a-uuid"})

    assert resolver.calls == []


def test_anonymous_never_passes(guard):
    _assert_hidden(guard, ANONYMOUS, ANY_RULE, path={"booking_id": BOOKING_ID})


def test_absent_or_null_value_is_skipped(guard, resolver):
    guest = _principal(GUEST_ID, Role.GUEST)

    _check(guard, guest, GUEST_RULE, path={})
    _check(guard, guest, IdorRule("booking", "booking_id", source="body"), body={"booking_id": None})

    assert resolver.calls == []


def test_body_list_is_checked_element_wise(guard):
    rule = IdorRule(resource="booking", key="booking_ids", source="body", relations=frozenset({"guest"}))
    guest = _principal(GUEST_ID, Role.GUEST)

    _check(guard, guest, rule, body={"booking_ids": [str(BOOKING_ID)]})
    _assert_hidden(guard, guest, rule, body={"booking_ids": [str(BOOKING_ID), str(OTHER_BOOKING_ID)]})


def test_every_repeated_query_value_is_checked(guard):
    rule = IdorRule(resource="booking", key="booking_id", source="query", relations=frozenset({"guest"}))
    query = MultiValueQuery(booking_id=[str(BOOKING_ID), str(OTHER_BOOKING_ID)])

    _assert_hidden(guard, _principal(GUEST_ID, Role.GUEST), rule, query=query)


def test_can_access_matches_role_relation_and_user():
    ownership = Ownership(guest_user_ids=frozenset({"u1"}), owner_user_ids=frozenset({"u2"}))

    assert IdorGuard.can_access(_principal("u1", Role.GUEST), ownership, frozenset({"guest"}))
    assert not IdorGuard.can_access(_principal("u1", Role.GUEST), ownership, frozenset({"owner"}))
    assert IdorGuard.can_access(_principal("u2", Role.OWNER), ownership, frozenset({"owner"}))
    assert not IdorGuard.can_access(_principal("u2", Role.GUEST, Role.OWNER), ownership, frozenset({"guest"}))


def test_unregistered_resource_fails_closed(guard):
    rule = IdorRule(resource="invoice", key="invoice_id")

    with pytest.raises(LookupError):
        _check(guard, _principal(GUEST_ID, Role.GUEST), rule, path={"invoice_id": uuid4()})


def test_registry_refuses_a_second_resolver_type_for_a_resource(resolver):
    class OtherResolver(DictResolver):
        pass

    registry = OwnershipRegistry()
    registry.register("booking", resolver)
    registry.register("booking", DictResolver({}))  # ready() running again re-registers the same kind

    with pytest.raises(ValueError):
        registry.register("booking", OtherResolver({}))
