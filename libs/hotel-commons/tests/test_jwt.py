"""JWT bearer tokens (LLD §3.7): HS256 only, every registered claim required, strict subject and roles.

Pure: ``JwtTokenService`` is built from explicit keys, so no Django settings are needed.
"""

import time

import jwt
import pytest

from hotel_commons.exceptions import Unauthenticated
from hotel_commons.security.jwt_tokens import JwtTokenService
from hotel_commons.security.principal import Principal, Role
from hotel_commons.status_code import TOKEN_EXPIRED, UNAUTHENTICATED

KEY = b"k" * 32
OTHER_KEY = b"o" * 32
ISSUER = "hotel-booking"
AUDIENCE = "hotel-booking-api"


@pytest.fixture
def service() -> JwtTokenService:
    return JwtTokenService(signing_key=KEY, issuer=ISSUER, audience=AUDIENCE, ttl_seconds=600, leeway_seconds=0)


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "guest-1",
        "roles": ["GUEST"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": "jti-1",
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _signed(claims: dict[str, object], key: bytes = KEY) -> str:
    return jwt.encode(claims, key, algorithm="HS256")


def _verify_fails_with(service: JwtTokenService, token: str):
    with pytest.raises(Unauthenticated) as error:
        service.verify(token)
    return error.value.status_code


def test_issued_token_verifies_to_its_principal(service):
    token = service.issue(user_id="guest-1", roles=[Role.GUEST, Role.OWNER])

    principal = service.verify(token)

    assert principal == Principal(user_id="guest-1", roles=frozenset({Role.GUEST, Role.OWNER}))
    claims = jwt.decode(token, KEY, algorithms=["HS256"], audience=AUDIENCE)
    assert set(claims) == {"sub", "roles", "iss", "aud", "iat", "nbf", "exp", "jti"}
    assert claims["exp"] - claims["iat"] == 600


def test_issue_honours_a_per_token_ttl(service):
    claims = jwt.decode(service.issue(user_id="guest-1", roles=[Role.GUEST], ttl_seconds=60), KEY,
                        algorithms=["HS256"], audience=AUDIENCE)

    assert claims["exp"] - claims["iat"] == 60


def test_expired_token_is_token_expired(service):
    past = int(time.time()) - 3600
    token = _signed(_claims(iat=past, nbf=past, exp=past + 60))

    assert _verify_fails_with(service, token) == TOKEN_EXPIRED


def test_wrong_key_is_unauthenticated(service):
    assert _verify_fails_with(service, _signed(_claims(), key=OTHER_KEY)) == UNAUTHENTICATED


def test_alg_none_is_unauthenticated(service):
    token = jwt.encode(_claims(), None, algorithm="none")

    assert _verify_fails_with(service, token) == UNAUTHENTICATED


def test_other_hmac_algorithm_is_unauthenticated(service):
    token = jwt.encode(_claims(), KEY * 2, algorithm="HS512")

    assert _verify_fails_with(service, token) == UNAUTHENTICATED


@pytest.mark.parametrize("claim", ["aud", "iss", "exp", "iat", "nbf", "sub", "roles"])
def test_missing_required_claim_is_unauthenticated(service, claim):
    assert _verify_fails_with(service, _signed(_claims(**{claim: None}))) == UNAUTHENTICATED


@pytest.mark.parametrize(("claim", "value"), [("aud", "another-api"), ("iss", "someone-else")])
def test_foreign_audience_or_issuer_is_unauthenticated(service, claim, value):
    assert _verify_fails_with(service, _signed(_claims(**{claim: value}))) == UNAUTHENTICATED


@pytest.mark.parametrize("subject", ["g" * 65, "guest 1", "guest/1", ""])
def test_malformed_or_oversized_subject_is_unauthenticated(service, subject):
    assert _verify_fails_with(service, _signed(_claims(sub=subject))) == UNAUTHENTICATED


def test_unknown_roles_are_dropped(service):
    principal = service.verify(_signed(_claims(roles=["GUEST", "SUPERUSER", 7])))

    assert principal.roles == frozenset({Role.GUEST})


@pytest.mark.parametrize("roles", [["SUPERUSER"], [], "GUEST", {"role": "GUEST"}])
def test_no_known_role_or_non_list_roles_is_unauthenticated(service, roles):
    assert _verify_fails_with(service, _signed(_claims(roles=roles))) == UNAUTHENTICATED


def test_garbage_token_is_unauthenticated(service):
    assert _verify_fails_with(service, "not-a-jwt") == UNAUTHENTICATED


def test_not_yet_valid_token_is_unauthenticated(service):
    future = int(time.time()) + 3600
    token = _signed(_claims(nbf=future, exp=future + 600))

    assert _verify_fails_with(service, token) == UNAUTHENTICATED


def test_short_signing_key_is_refused():
    with pytest.raises(ValueError):
        JwtTokenService(signing_key=b"short", issuer=ISSUER, audience=AUDIENCE)


@pytest.mark.parametrize(("user_id", "roles"), [("bad subject", [Role.GUEST]), ("guest-1", [])])
def test_issue_refuses_tokens_that_could_never_verify(service, user_id, roles):
    with pytest.raises(ValueError):
        service.issue(user_id=user_id, roles=roles)
