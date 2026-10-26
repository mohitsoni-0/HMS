"""Idempotency-Key handling (LLD §3.11): the ``idempotent`` decorator and the encrypted Redis store.

No database. Without the project settings (``-p no:django``) a minimal Django is configured here; either
way every setting the pipeline reads is overridden per test, so the results do not depend on which
settings loaded first.
"""

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework"],
        USE_TZ=True,
        TIME_ZONE="Asia/Kolkata",
        ALLOWED_HOSTS=["testserver"],
        REST_FRAMEWORK={"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema", "UNAUTHENTICATED_USER": None},
    )
django.setup()

import json  # noqa: E402

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from django.test import override_settings  # noqa: E402
from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from hotel_commons.api.authentication import PrincipalAuthentication  # noqa: E402
from hotel_commons.exceptions import Conflict  # noqa: E402
from hotel_commons.idempotency import decorators  # noqa: E402
from hotel_commons.idempotency.decorators import idempotent  # noqa: E402
from hotel_commons.idempotency.interfaces import BeginOutcome, IdempotencyStore, StoredResponse  # noqa: E402
from hotel_commons.idempotency.redis_store import RedisIdempotencyStore, get_idempotency_store  # noqa: E402
from hotel_commons.redis.client import get_redis  # noqa: E402
from hotel_commons.security.pii import PiiCipher, get_pii_cipher  # noqa: E402
from hotel_commons.security.principal import Principal, Role  # noqa: E402
from hotel_commons.status_code import (  # noqa: E402
    IDEMPOTENCY_IN_PROGRESS,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REQUIRED,
    IDEMPOTENCY_KEY_REUSED,
    StatusCode,
)
from hotel_commons.testing.stores import InMemoryIdempotencyStore  # noqa: E402

PIPELINE_SETTINGS = {
    "REDIS_URL": "fakeredis://",
    "IDEMPOTENCY_FINGERPRINT_KEY": b"f" * 32,
    "PII_ENCRYPTION_KEYS": {"t1": b"p" * 32},
    "PII_ACTIVE_KEY_ID": "t1",
    "REST_FRAMEWORK": {
        "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
        "EXCEPTION_HANDLER": "hotel_commons.api.exception_handler.envelope_exception_handler",
        "UNAUTHENTICATED_USER": None,
    },
}
GUEST = Principal(user_id="guest-1", roles=frozenset({Role.GUEST}))
OTHER_GUEST = Principal(user_id="guest-2", roles=frozenset({Role.GUEST}))
KEY = "pay-0192f5c5-attempt-1"
PATH = "/hotel/v1/bookings/0192f5c5/payments"
BOOKING_NOT_PAYABLE = StatusCode(2105002, "Cannot pay a booking that is {state}", 409)


@pytest.fixture(autouse=True)
def pipeline_settings():
    with override_settings(**PIPELINE_SETTINGS):
        _clear_caches()
        get_redis().flushall()  # always the in-process FakeRedis here: REDIS_URL is overridden above
        yield
    _clear_caches()


def _clear_caches() -> None:
    for cached in (get_redis, get_pii_cipher, get_idempotency_store):
        cached.cache_clear()


@pytest.fixture
def store(monkeypatch) -> IdempotencyStore:
    in_memory = InMemoryIdempotencyStore()
    monkeypatch.setattr(decorators, "get_idempotency_store", lambda: in_memory)
    return in_memory


class PayView(APIView):
    """Counts its runs; answers with the status and body the test asks for, or raises what it is told to."""

    authentication_classes = [PrincipalAuthentication]
    permission_classes: list = []
    throttle_classes: list = []
    runs = 0
    reply_status = 200
    raise_error: Exception | None = None

    @idempotent("booking_pay")
    def post(self, request):
        type(self).runs += 1
        if self.raise_error is not None:
            raise self.raise_error
        return Response({"run": type(self).runs, "key": request.idempotency_key}, status=self.reply_status)


class OptionalKeyView(PayView):
    @idempotent("booking_create", required=False)
    def post(self, request):
        type(self).runs += 1
        return Response({"run": type(self).runs}, status=201)


@pytest.fixture(autouse=True)
def reset_views():
    for view in (PayView, OptionalKeyView):
        view.runs, view.reply_status, view.raise_error = 0, 200, None


def _call(view=PayView, *, key: str | None = KEY, body=None, principal: Principal = GUEST):
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
    request = APIRequestFactory().post(PATH, body if body is not None else {"method": "UPI"}, format="json", **headers)
    request.principal = principal
    return view.as_view()(request)


def test_replay_returns_the_stored_response_without_running_the_view(store):
    first = _call()
    second = _call()

    assert PayView.runs == 1
    assert (second.status_code, second.data) == (first.status_code, first.data) == (200, {"run": 1, "key": KEY})
    assert second["Idempotent-Replayed"] == "true"
    assert not first.has_header("Idempotent-Replayed")


def test_in_progress_is_409(monkeypatch):
    class InProgressStore(InMemoryIdempotencyStore):
        def begin(self, key, fingerprint, lock_ttl_seconds):
            return BeginOutcome(kind="in_progress")

    monkeypatch.setattr(decorators, "get_idempotency_store", InProgressStore)

    response = _call()

    assert (response.status_code, response.data["status"]) == (409, IDEMPOTENCY_IN_PROGRESS.code)
    assert PayView.runs == 0


def test_same_key_with_another_body_is_422(store):
    _call(body={"method": "UPI"})

    response = _call(body={"method": "CARD"})

    assert (response.status_code, response.data["status"]) == (422, IDEMPOTENCY_KEY_REUSED.code)
    assert PayView.runs == 1


def test_fingerprint_ignores_key_order(store):
    _call(body={"method": "UPI", "upi": {"vpa": "a@b"}})

    replay = _call(body={"upi": {"vpa": "a@b"}, "method": "UPI"})

    assert replay["Idempotent-Replayed"] == "true"


def test_keys_are_scoped_per_user(store):
    _call()
    _call(principal=OTHER_GUEST)

    assert PayView.runs == 2


def test_app_error_4xx_is_stored_and_replayed(store):
    PayView.raise_error = Conflict(BOOKING_NOT_PAYABLE, state="CANCELLED")

    first = _call()
    second = _call()

    assert PayView.runs == 1
    assert first.status_code == second.status_code == 409
    assert first.data["message"] == "Cannot pay a booking that is CANCELLED"
    assert second.data == first.data and second["Idempotent-Replayed"] == "true"


def test_5xx_response_releases_the_key(store):
    PayView.reply_status = 503
    assert _call().status_code == 503

    PayView.reply_status = 200
    retried = _call()

    assert (retried.status_code, PayView.runs) == (200, 2)
    assert not retried.has_header("Idempotent-Replayed")


def test_unexpected_exception_releases_the_key_and_propagates(store):
    PayView.raise_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        _call()

    PayView.raise_error = None
    assert _call().status_code == 200
    assert PayView.runs == 2


def test_redis_error_still_runs_the_view(monkeypatch):
    class DownStore(InMemoryIdempotencyStore):
        def begin(self, key, fingerprint, lock_ttl_seconds):
            raise RedisConnectionError("redis is down")

    monkeypatch.setattr(decorators, "get_idempotency_store", DownStore)

    assert _call().status_code == 200
    assert _call().status_code == 200
    assert PayView.runs == 2


def test_redis_error_on_complete_is_ignored(monkeypatch):
    class FlakyStore(InMemoryIdempotencyStore):
        def complete(self, key, fingerprint, response, ttl_seconds):
            raise RedisConnectionError("redis is down")

    monkeypatch.setattr(decorators, "get_idempotency_store", FlakyStore)

    assert _call().status_code == 200


def test_missing_key_is_400_when_required(store):
    response = _call(key=None)

    assert (response.status_code, response.data["status"]) == (400, IDEMPOTENCY_KEY_REQUIRED.code)
    assert PayView.runs == 0


def test_missing_key_runs_the_view_when_optional(store):
    assert _call(OptionalKeyView, key=None).status_code == 201
    assert _call(OptionalKeyView, key=None).status_code == 201
    assert OptionalKeyView.runs == 2


@pytest.mark.parametrize("key", ["short", "x" * 129, "has space-12345", "semi;colon-1234", ""])
def test_malformed_key_is_400(store, key):
    response = _call(key=key)

    assert (response.status_code, response.data["status"]) == (400, IDEMPOTENCY_KEY_INVALID.code)


def test_redis_store_value_is_encrypted_and_replays():
    first = _call()
    second = _call()

    raw = get_redis().get(f"hb:idem:v1:booking_pay:{GUEST.user_id}:{KEY}")
    assert raw.startswith("ENC_")
    assert PayView.runs == 1 and second.data == first.data and second["Idempotent-Replayed"] == "true"


def test_redis_store_outcomes():
    store = RedisIdempotencyStore(fakeredis.FakeRedis(decode_responses=True), PiiCipher({"t1": b"p" * 32}, "t1"))

    assert store.begin("k", "fp", 60).kind == "started"
    assert store.begin("k", "fp", 60).kind == "in_progress"
    assert store.begin("k", "other", 60).kind == "mismatch"
    store.complete("k", "fp", StoredResponse(http_status=201, body={"id": "b-1"}), 3600)
    assert store.begin("k", "fp", 60) == BeginOutcome(kind="replay", response=StoredResponse(201, {"id": "b-1"}))
    store.release("k")
    assert store.begin("k", "fp", 60).kind == "started"


def test_redis_store_ttls():
    client = fakeredis.FakeRedis(decode_responses=True)
    store = RedisIdempotencyStore(client, PiiCipher({"t1": b"p" * 32}, "t1"))

    store.begin("k", "fp", 60)
    lock_ttl = client.ttl("k")
    store.complete("k", "fp", StoredResponse(http_status=200, body={}), 86_400)

    assert 0 < lock_ttl <= 60
    assert 60 < client.ttl("k") <= 86_400


def test_stored_record_is_bound_to_the_idempotency_aad():
    cipher = PiiCipher({"t1": b"p" * 32}, "t1")
    client = fakeredis.FakeRedis(decode_responses=True)
    RedisIdempotencyStore(client, cipher).complete("k", "fp", StoredResponse(http_status=200, body={"a": 1}), 60)

    record = json.loads(cipher.decrypt(client.get("k"), aad="idem"))

    assert record == {"state": "completed", "fingerprint": "fp", "http_status": 200, "body": {"a": 1}}
