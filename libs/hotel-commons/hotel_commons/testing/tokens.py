"""Bearer-token helpers for API tests.

``hotel_commons.security.jwt_tokens`` belongs to the security track and reads Django settings, so it is
imported when a token is made rather than when this module is imported.
"""

from hotel_commons.security.principal import Role

DEFAULT_TTL_SECONDS = 3600


def make_token(user_id: str, *roles: Role | str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """A token for ``user_id`` with ``roles`` (members or names), signed by the configured JwtTokenService."""
    from hotel_commons.security.jwt_tokens import get_token_service

    return get_token_service().issue(user_id=user_id, roles=[Role(role) for role in roles], ttl_seconds=ttl_seconds)


def auth_header(user_id: str, *roles: Role | str) -> dict[str, str]:
    """The header as test-client kwargs: ``api_client.get(url, **auth_header("guest-1", Role.GUEST))``."""
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user_id, *roles)}"}
