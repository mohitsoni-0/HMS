"""``@idempotent(scope)`` for DRF view methods (LLD §3.11).

A retried request carrying the same ``Idempotency-Key`` gets the first response back instead of running
the use case again. Keys are scoped per user and endpoint scope; a fingerprint (HMAC of method, path and
canonical body) catches a key reused for a different request. The Redis layer is an optimisation: when
Redis is unreachable the view runs anyway, and the database's unique idempotency keys keep writes single.

Responses below 500, returned or raised as ``AppError``, are stored and replayed; a 5xx or an unexpected
exception releases the key so the client can retry.
"""

import functools
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Final

from django.conf import settings
from django.http import QueryDict
from redis.exceptions import RedisError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

from hotel_commons.api.exception_handler import envelope_exception_handler
from hotel_commons.consts import IDEMPOTENCY_KEY_HEADER, IDEMPOTENT_REPLAYED_HEADER
from hotel_commons.context import RequestContext
from hotel_commons.exceptions import AppError, BusinessRuleViolation, Conflict, ValidationFailed
from hotel_commons.idempotency.interfaces import IdempotencyStore, StoredResponse
from hotel_commons.idempotency.redis_store import get_idempotency_store
from hotel_commons.redis.client import redis_key
from hotel_commons.security.permissions import principal_of
from hotel_commons.status_code import (
    IDEMPOTENCY_IN_PROGRESS,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REQUIRED,
    IDEMPOTENCY_KEY_REUSED,
)

logger = logging.getLogger(__name__)

KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{8,128}")
SERVER_ERROR_STATUS: Final = 500

ViewMethod = Callable[..., Response]


def idempotent(
    scope: str, *, required: bool = True, ttl_seconds: int = 86_400, lock_ttl_seconds: int = 60
) -> Callable[[ViewMethod], ViewMethod]:
    """Make a view method idempotent per ``Idempotency-Key``; ``required=False`` lets keyless calls through."""

    def decorate(view_method: ViewMethod) -> ViewMethod:
        @functools.wraps(view_method)
        def wrapper(view: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
            key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
            if key is None:
                if required:
                    raise ValidationFailed(IDEMPOTENCY_KEY_REQUIRED)
                return view_method(view, request, *args, **kwargs)
            if not KEY_PATTERN.fullmatch(key):
                raise ValidationFailed(IDEMPOTENCY_KEY_INVALID)
            request.idempotency_key = key
            call = _IdempotentCall(
                store=get_idempotency_store(),
                store_key=redis_key("idem", "v1", scope, principal_of(request).user_id, key),
                fingerprint=_fingerprint(request),
                scope=scope,
            )
            return call.run(lambda: view_method(view, request, *args, **kwargs), view, request,
                            ttl_seconds=ttl_seconds, lock_ttl_seconds=lock_ttl_seconds)

        return wrapper

    return decorate


class _IdempotentCall:
    """One request under one key: claim the key, then replay, refuse or run and record."""

    def __init__(self, *, store: IdempotencyStore, store_key: str, fingerprint: str, scope: str) -> None:
        self._store = store
        self._key = store_key
        self._fingerprint = fingerprint
        self._scope = scope

    def run(
        self, view_call: Callable[[], Response], view: Any, request: Request, *, ttl_seconds: int, lock_ttl_seconds: int
    ) -> Response:
        try:
            outcome = self._store.begin(self._key, self._fingerprint, lock_ttl_seconds)
        except RedisError:
            logger.warning("Idempotency store unavailable for scope %s; running without it", self._scope)
            return view_call()
        if outcome.kind == "replay" and outcome.response is not None:
            return _replayed(outcome.response)
        if outcome.kind == "in_progress":
            raise Conflict(IDEMPOTENCY_IN_PROGRESS)
        if outcome.kind == "mismatch":
            raise BusinessRuleViolation(IDEMPOTENCY_KEY_REUSED)
        return self._run_and_record(view_call, view, request, ttl_seconds)

    def _run_and_record(
        self, view_call: Callable[[], Response], view: Any, request: Request, ttl_seconds: int
    ) -> Response:
        try:
            response = view_call()
        except AppError as exc:
            response = envelope_exception_handler(exc, {"view": view, "request": request})
        except Exception:
            self._quietly(self._store.release, self._key)
            raise
        body = getattr(response, "data", None)
        if response.status_code >= SERVER_ERROR_STATUS or not isinstance(body, dict):
            self._quietly(self._store.release, self._key)
            return response
        stored = StoredResponse(http_status=response.status_code, body=body)
        self._quietly(self._store.complete, self._key, self._fingerprint, stored, ttl_seconds)
        return response

    def _quietly(self, operation: Callable[..., None], *args: Any) -> None:
        try:
            operation(*args)
        except RedisError:
            logger.warning("Idempotency store unavailable for scope %s; result not recorded", self._scope)


def _fingerprint(request: Request) -> str:
    """HMAC-SHA256 of ``METHOD|path|canonical JSON body`` under IDEMPOTENCY_FINGERPRINT_KEY (hex)."""
    data = request.data
    if isinstance(data, QueryDict):  # no body: DRF hands back an empty QueryDict
        data = {name: values for name, values in data.lists()}
    canonical = json.dumps(data, cls=JSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    message = f"{request.method}|{request.path}|{canonical}".encode()
    return hmac.new(settings.IDEMPOTENCY_FINGERPRINT_KEY, message, hashlib.sha256).hexdigest()


def _replayed(stored: StoredResponse) -> Response:
    """The stored response, re-stamped with this request's id so logs and clients can correlate the retry."""
    body = dict(stored.body)
    if "request_id" in body:
        body["request_id"] = RequestContext.request_id()
    return Response(body, status=stored.http_status, headers={IDEMPOTENT_REPLAYED_HEADER: "true"})
