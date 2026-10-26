"""Signed bearer tokens (LLD §3.7): HS256 JWTs carrying the caller's id and roles.

Verification is deliberately narrow: one algorithm (so ``alg=none`` and algorithm confusion are refused),
every registered claim required, our issuer and audience only, a bounded subject and at least one known
role. Every failure is ``Unauthenticated``; only an expired token says so (TOKEN_EXPIRED), anything else is
the generic UNAUTHENTICATED, so a caller learns nothing about why a forged token failed.
"""

import logging
import re
import time
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Final, Protocol
from uuid import uuid4

import jwt
from django.conf import settings

from hotel_commons.exceptions import Unauthenticated
from hotel_commons.security.principal import Principal, Role
from hotel_commons.status_code import TOKEN_EXPIRED, UNAUTHENTICATED

logger = logging.getLogger(__name__)

ALGORITHM: Final = "HS256"
MIN_SIGNING_KEY_BYTES: Final = 32
REQUIRED_CLAIMS: Final = ("exp", "iat", "nbf", "iss", "aud", "sub", "roles")
SUBJECT_PATTERN: Final = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_KNOWN_ROLES: Final = frozenset(role.value for role in Role)


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class JwtTokenService:
    """Issues and verifies HS256 tokens for one issuer and audience."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        issuer: str,
        audience: str,
        ttl_seconds: int = 3600,
        leeway_seconds: int = 30,
    ) -> None:
        if len(signing_key) < MIN_SIGNING_KEY_BYTES:
            raise ValueError(f"The JWT signing key must be at least {MIN_SIGNING_KEY_BYTES} bytes")
        self._key = bytes(signing_key)
        self._issuer = issuer
        self._audience = audience
        self._ttl_seconds = ttl_seconds
        self._leeway_seconds = leeway_seconds

    @property
    def ttl_seconds(self) -> int:
        """Lifetime of a token issued without an explicit ``ttl_seconds``."""
        return self._ttl_seconds

    def issue(self, *, user_id: str, roles: Iterable[Role], ttl_seconds: int | None = None) -> str:
        """A signed token; ValueError when ``user_id`` or ``roles`` would make it unverifiable."""
        if not SUBJECT_PATTERN.fullmatch(user_id):
            raise ValueError("user_id must match ^[A-Za-z0-9_.@-]{1,64}$")
        role_names = sorted(Role(role).value for role in roles)
        if not role_names:
            raise ValueError("A token needs at least one role")
        now = int(time.time())
        claims = {
            "sub": user_id,
            "roles": role_names,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "nbf": now,
            "exp": now + (self._ttl_seconds if ttl_seconds is None else ttl_seconds),
            "jti": uuid4().hex,
        }
        return jwt.encode(claims, self._key, algorithm=ALGORITHM)

    def verify(self, token: str) -> Principal:
        """The token's principal, or ``Unauthenticated`` (TOKEN_EXPIRED or UNAUTHENTICATED)."""
        claims = self._decode(token)
        subject = claims["sub"]
        if not isinstance(subject, str) or not SUBJECT_PATTERN.fullmatch(subject):
            raise _rejected("malformed subject")
        return Principal(user_id=subject, roles=_known_roles(claims["roles"]))

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError:
            raise Unauthenticated(TOKEN_EXPIRED) from None
        except jwt.PyJWTError as exc:
            raise _rejected(type(exc).__name__) from None


def _known_roles(roles: object) -> frozenset[Role]:
    """The recognised roles of the ``roles`` claim; unknown names are dropped, none left is a rejection."""
    if not isinstance(roles, list):
        raise _rejected("roles claim is not a list")
    known = frozenset(Role(role) for role in roles if isinstance(role, str) and role in _KNOWN_ROLES)
    if not known:
        raise _rejected("no known role")
    return known


def _rejected(reason: str) -> Unauthenticated:
    logger.info("Bearer token rejected: %s", reason)
    return Unauthenticated(UNAUTHENTICATED)


@lru_cache(maxsize=1)
def get_token_service() -> JwtTokenService:
    """The process-wide service from the ``JWT_*`` settings (``cache_clear()`` after changing them)."""
    return JwtTokenService(
        signing_key=settings.JWT_SIGNING_KEY,
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
        ttl_seconds=settings.JWT_TTL_SECONDS,
        leeway_seconds=settings.JWT_LEEWAY_SECONDS,
    )
