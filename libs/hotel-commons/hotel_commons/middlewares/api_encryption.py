"""Payload decryption and response encryption for the API (LLD §3.7 wire format v1, §3.8 row 4).

``API_ENCRYPTION_MODE``:
- ``off``: requests and responses pass through untouched, encryption headers included.
- ``optional``: requests with the v1 headers are decrypted and answered encrypted; plaintext passes.
- ``enforced``: like ``optional``, but a request without the headers gets 400 ``PAYLOAD_NOT_ENCRYPTED``.
Paths in ``API_ENCRYPTION_BYPASS_PATHS`` (exact match) always pass in plaintext.

An encrypted request is rate-limited by client IP before any RSA work (``RATE_LIMIT_POLICIES["encrypted"]``),
then its timestamp is checked (±300 s), its AES key unwrapped and claimed once in Redis (replay guard), and
its body decrypted into ``request._body`` as JSON. The AAD binds method, path, query string and timestamp,
and every method but GET/HEAD/OPTIONS must carry a sealed body, so a state-changing request cannot be
re-targeted, re-timed or stripped of its body. The view's JSON response, success or error, is sealed with
the same key. This middleware's own rejections are plain ``JsonResponse`` envelopes, never exceptions, and
are not encrypted: the client must be able to read why its request was refused.

The replay guard keeps each wrapped key for ``REPLAY_TTL_SECONDS``: a timestamp is accepted from
``ts - skew`` to ``ts + skew``, so the guard must outlive that whole 2 × skew window, plus a margin for
clock drift between app instances. A bodyless (GET/HEAD/OPTIONS) request has no sealed payload, so its
timestamp header is not authenticated; such a request changes nothing and its response is sealed with a
key only the original client holds.
"""

import hashlib
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from redis.exceptions import RedisError

from hotel_commons.api.response import error_body
from hotel_commons.api.throttling import rate_limit_policy
from hotel_commons.consts import (
    API_ENCRYPTION_ENFORCED,
    API_ENCRYPTION_OFF,
    ENCRYPTED_KEY_HEADER,
    PAYLOAD_ENCRYPTION_HEADER,
    PAYLOAD_TIMESTAMP_HEADER,
    RETRY_AFTER_HEADER,
)
from hotel_commons.exceptions import AppError, TooManyRequests, ValidationFailed
from hotel_commons.ratelimit.interfaces import RateLimiter
from hotel_commons.ratelimit.redis_sliding_window import RedisSlidingWindowRateLimiter
from hotel_commons.redis.client import get_redis, redis_key
from hotel_commons.security.api_crypto import (
    MAX_CLOCK_SKEW_SECONDS,
    PAYLOAD_FIELD,
    WIRE_VERSION,
    aad_target,
    get_payload_cipher,
    payload_aad,
    requires_sealed_body,
)
from hotel_commons.security.client_ip import client_ip
from hotel_commons.status_code import PAYLOAD_DECRYPTION_FAILED, PAYLOAD_NOT_ENCRYPTED, RATE_LIMITED

logger = logging.getLogger(__name__)

ENCRYPTED_SCOPE: Final = "encrypted"
REPLAY_TTL_MARGIN_SECONDS: Final = 60
REPLAY_TTL_SECONDS: Final = 2 * MAX_CLOCK_SKEW_SECONDS + REPLAY_TTL_MARGIN_SECONDS
MAX_TIMESTAMP_DIGITS: Final = 12  # unix seconds stay 10 digits until the year 2286
JSON_CONTENT_TYPE: Final = "application/json"
_ENCRYPTION_HEADERS: Final = (PAYLOAD_ENCRYPTION_HEADER, ENCRYPTED_KEY_HEADER, PAYLOAD_TIMESTAMP_HEADER)


@dataclass(frozen=True)
class EncryptedExchange:
    """The decrypted request's AES key and AAD parts, kept on ``request.encrypted_exchange`` for the response.

    ``target`` is the URL path plus ``?`` and the raw query string when there is one.
    """

    aes_key: bytes = field(repr=False)
    method: str
    target: str
    timestamp: int

    def request_aad(self) -> str:
        return payload_aad("req", self.method, self.target, self.timestamp)

    def response_aad(self) -> str:
        return payload_aad("res", self.method, self.target, self.timestamp)


class ApiEncryptionMiddleware:
    """Decrypts v1-encrypted requests and encrypts their JSON responses; see the module docstring."""

    def __init__(
        self, get_response: Callable[[HttpRequest], HttpResponse], *, rate_limiter: RateLimiter | None = None
    ) -> None:
        self.get_response = get_response
        self._rate_limiter = rate_limiter

    def __call__(self, request: HttpRequest) -> HttpResponse:
        mode = settings.API_ENCRYPTION_MODE
        if mode == API_ENCRYPTION_OFF or request.path in settings.API_ENCRYPTION_BYPASS_PATHS:
            return self.get_response(request)
        if not _carries_encryption_headers(request):
            if mode == API_ENCRYPTION_ENFORCED:
                logger.info("Plaintext request refused in enforced mode: %s %s", request.method, request.path)
                return _error_response(ValidationFailed(PAYLOAD_NOT_ENCRYPTED))
            return self.get_response(request)
        try:
            self._check_rate_limit(request)
            exchange = _open_request(request)
        except AppError as error:
            return _error_response(error)
        request.encrypted_exchange = exchange
        return _seal_response(self.get_response(request), exchange)

    def _check_rate_limit(self, request: HttpRequest) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return
        decision = self._limiter().hit(f"ip:{client_ip(request)}", rate_limit_policy(ENCRYPTED_SCOPE))
        if not decision.allowed:
            raise TooManyRequests(RATE_LIMITED, retry_after_seconds=decision.retry_after_seconds)

    def _limiter(self) -> RateLimiter:
        if self._rate_limiter is None:
            self._rate_limiter = RedisSlidingWindowRateLimiter(get_redis())
        return self._rate_limiter


def _carries_encryption_headers(request: HttpRequest) -> bool:
    return any(name in request.headers for name in _ENCRYPTION_HEADERS)


def _open_request(request: HttpRequest) -> EncryptedExchange:
    """Checks in the order of the wire format: version, timestamp, body presence, key unwrap, replay, decryption.

    A method that must carry a body (see ``requires_sealed_body``) is refused when the body is empty.
    """
    if request.headers.get(PAYLOAD_ENCRYPTION_HEADER) != WIRE_VERSION:
        raise _rejected("missing or unsupported encryption version")
    wrapped_key = request.headers.get(ENCRYPTED_KEY_HEADER, "")
    if not wrapped_key:
        raise _rejected("missing wrapped key")
    timestamp = _fresh_timestamp(request.headers.get(PAYLOAD_TIMESTAMP_HEADER, ""))
    if not request.body and requires_sealed_body(request.method):
        raise _rejected("sealed body missing")
    cipher = get_payload_cipher()
    exchange = EncryptedExchange(
        aes_key=cipher.unwrap_key(wrapped_key),
        method=request.method,
        target=aad_target(request.path, request.META.get("QUERY_STRING", "")),
        timestamp=timestamp,
    )
    _claim_wrapped_key(wrapped_key)
    if request.body:
        sealed = _sealed_payload(request.body)
        _replace_body(request, cipher.decrypt(exchange.aes_key, sealed, aad=exchange.request_aad()))
    return exchange


def _fresh_timestamp(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > MAX_TIMESTAMP_DIGITS:
        raise _rejected("timestamp is not unix seconds")
    timestamp = int(raw)
    if abs(time.time() - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise _rejected("timestamp outside the allowed clock skew")
    return timestamp


def _claim_wrapped_key(wrapped_key: str) -> None:
    """Accept each wrapped key once; a Redis outage disables the guard rather than the API (fail open)."""
    key = redis_key("enc", WIRE_VERSION, hashlib.sha256(wrapped_key.encode()).hexdigest())
    try:
        claimed = get_redis().set(key, 1, nx=True, ex=REPLAY_TTL_SECONDS)
    except RedisError:
        logger.warning("Encrypted-payload replay guard unavailable; request accepted without it")
        return
    if not claimed:
        raise _rejected("wrapped key replayed")


def _sealed_payload(body: bytes) -> str:
    try:
        document = json.loads(body)
    except ValueError:
        raise _rejected("body is not JSON") from None
    payload = document.get(PAYLOAD_FIELD) if isinstance(document, dict) else None
    if not isinstance(payload, str):
        raise _rejected("body has no payload")
    return payload


def _replace_body(request: HttpRequest, plaintext: bytes) -> None:
    request._body = plaintext
    request._stream = io.BytesIO(plaintext)
    request.META["CONTENT_TYPE"] = JSON_CONTENT_TYPE
    request.META["CONTENT_LENGTH"] = str(len(plaintext))
    request.content_type = JSON_CONTENT_TYPE
    request.content_params = {}


def _seal_response(response: HttpResponse, exchange: EncryptedExchange) -> HttpResponse:
    """Encrypt a non-empty JSON response in place; anything else (streams, empty or non-JSON bodies) passes."""
    is_json = response.get("Content-Type", "").startswith(JSON_CONTENT_TYPE)
    if getattr(response, "streaming", False) or not is_json or not response.content:
        return response
    sealed = get_payload_cipher().encrypt(exchange.aes_key, response.content, aad=exchange.response_aad())
    response.content = json.dumps({PAYLOAD_FIELD: sealed}).encode()
    response[PAYLOAD_ENCRYPTION_HEADER] = WIRE_VERSION
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def _error_response(error: AppError) -> JsonResponse:
    response = JsonResponse(error_body(error.status_code, error.message, error.errors), status=error.http_status)
    if isinstance(error, TooManyRequests):
        response[RETRY_AFTER_HEADER] = str(error.retry_after_seconds)
    return response


def _rejected(reason: str) -> ValidationFailed:
    logger.warning("Encrypted API request rejected: %s", reason)
    return ValidationFailed(PAYLOAD_DECRYPTION_FAILED)
