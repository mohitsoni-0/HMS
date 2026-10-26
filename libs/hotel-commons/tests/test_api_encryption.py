"""API payload encryption (LLD §3.7 api_crypto, §3.8 row 4): the hybrid cipher, the client and the middleware.

No database. When the suite runs without the project settings (``-p no:django``), the module configures a
minimal Django itself; under the project settings it only overrides the encryption settings per test. A
3072-bit key is generated once for the module and served through ``API_RSA_PRIVATE_KEY_PATH``.
"""

import hashlib
import json
import time

import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-only-api-encryption",
        ALLOWED_HOSTS=["testserver"],
        INSTALLED_APPS=["rest_framework"],
        REDIS_URL="fakeredis://",
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_POLICIES={
            "default": {"limit": 120, "window_seconds": 60, "fail_open": True},
            "encrypted": {"limit": 300, "window_seconds": 60, "fail_open": True},
        },
        TRUSTED_PROXY_COUNT=0,
        API_ENCRYPTION_MODE="optional",
        API_ENCRYPTION_BYPASS_PATHS=frozenset(),
        API_RSA_PRIVATE_KEY_PATH="",
        REST_FRAMEWORK={
            "DEFAULT_AUTHENTICATION_CLASSES": [],
            "DEFAULT_PERMISSION_CLASSES": [],
            "DEFAULT_THROTTLE_CLASSES": [],
            # As base_settings: whichever module configures Django first, later modules generate OpenAPI schemas.
            "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
            "UNAUTHENTICATED_USER": None,
        },
    )
    django.setup()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from django.http import HttpRequest, HttpResponse, JsonResponse  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402

from hotel_commons.exceptions import ValidationFailed  # noqa: E402
from hotel_commons.redis.client import get_redis, redis_key  # noqa: E402
from hotel_commons.security import api_crypto  # noqa: E402
from hotel_commons.security.api_crypto import (  # noqa: E402
    ALGORITHM,
    MAX_CLOCK_SKEW_SECONDS,
    HybridPayloadCipher,
    HybridPayloadClient,
    aad_target,
    get_payload_cipher,
    payload_aad,
)
from hotel_commons.status_code import (  # noqa: E402
    PAYLOAD_DECRYPTION_FAILED,
    PAYLOAD_NOT_ENCRYPTED,
    RATE_LIMITED,
)

API_PATH = "/hotel/v1/bookings"
BYPASS_PATH = "/hotel/v1/health"


# --- fixtures -------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture(scope="module")
def key_path(private_key_pem, tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("api-rsa") / "api_rsa_private.pem"
    path.write_bytes(private_key_pem)
    return str(path)


@pytest.fixture
def encryption_settings(key_path):
    """Optional mode, one bypass path, the module key, a clean cipher cache and an empty fake Redis."""
    with override_settings(
        API_ENCRYPTION_MODE="optional",
        API_ENCRYPTION_BYPASS_PATHS=frozenset({BYPASS_PATH}),
        API_RSA_PRIVATE_KEY_PATH=key_path,
        REDIS_URL="fakeredis://",
        RATE_LIMIT_ENABLED=True,
    ):
        get_payload_cipher.cache_clear()
        get_redis.cache_clear()
        get_redis().flushall()
        yield
    get_payload_cipher.cache_clear()
    get_redis.cache_clear()


@pytest.fixture
def client(private_key_pem) -> HybridPayloadClient:
    return HybridPayloadClient(_public_pem(private_key_pem))


@pytest.fixture
def middleware(encryption_settings):
    """The middleware around an echo view that records what it received."""
    from hotel_commons.middlewares.api_encryption import ApiEncryptionMiddleware

    seen: list[HttpRequest] = []

    def echo_view(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        received = json.loads(request.body) if request.body else None
        return JsonResponse({"status": 2100000, "data": {"method": request.method, "received": received}})

    instance = ApiEncryptionMiddleware(echo_view)
    instance.seen = seen
    return instance


def _public_pem(private_key_pem: bytes) -> bytes:
    return HybridPayloadCipher(private_key_pem).public_key_pem().encode()


def _send(middleware, method: str, path: str, *, headers=None, body=None) -> HttpResponse:
    factory = RequestFactory()
    data = json.dumps(body) if body is not None else ""
    request = factory.generic(method, path, data=data, content_type="application/json", headers=headers or {})
    return middleware(request)


def _send_encrypted(middleware, client, method: str, path: str, body=None):
    encrypted = client.encrypt_request(method, path, body)
    response = _send(middleware, method, path, headers=encrypted.headers, body=encrypted.body)
    return encrypted, response


def _status(response: HttpResponse) -> int:
    return json.loads(response.content)["status"]


# --- cipher and client ----------------------------------------------------------------------------------------


def test_cipher_rejects_keys_under_3072_bits():
    small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = small.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )

    with pytest.raises(ValueError):
        HybridPayloadCipher(pem)


def test_client_wraps_a_key_the_server_unwraps(private_key_pem, client):
    cipher = HybridPayloadCipher(private_key_pem)
    encrypted = client.encrypt_request("post", API_PATH, {"rooms": 1})

    aes_key = cipher.unwrap_key(encrypted.headers["X-Encrypted-Key"])
    aad = payload_aad("req", "POST", API_PATH, encrypted.timestamp)

    assert aes_key == encrypted.aes_key
    assert encrypted.headers["X-Payload-Encryption"] == "v1"
    assert json.loads(cipher.decrypt(aes_key, encrypted.body["payload"], aad=aad)) == {"rooms": 1}
    assert "aes_key" not in repr(encrypted)


def test_aad_strings_follow_the_wire_format():
    assert payload_aad("req", "post", API_PATH, 1767000000) == f"v1|req|POST|{API_PATH}|1767000000"
    assert payload_aad("res", "GET", API_PATH, 5) == f"v1|res|GET|{API_PATH}|5"


def test_aad_target_appends_the_query_string_only_when_present():
    assert aad_target(API_PATH, "") == API_PATH
    assert aad_target(API_PATH, "property_id=a&page=2") == f"{API_PATH}?property_id=a&page=2"


def test_client_binds_the_query_string_into_the_aad(private_key_pem, client):
    cipher = HybridPayloadCipher(private_key_pem)
    encrypted = client.encrypt_request("POST", f"{API_PATH}?property_id=a", {"rooms": 1})
    aes_key = cipher.unwrap_key(encrypted.headers["X-Encrypted-Key"])

    assert encrypted.path == f"{API_PATH}?property_id=a"
    aad = payload_aad("req", "POST", f"{API_PATH}?property_id=a", encrypted.timestamp)
    assert json.loads(cipher.decrypt(aes_key, encrypted.body["payload"], aad=aad)) == {"rooms": 1}
    without_query = payload_aad("req", "POST", API_PATH, encrypted.timestamp)
    with pytest.raises(ValidationFailed):
        cipher.decrypt(aes_key, encrypted.body["payload"], aad=without_query)


@pytest.mark.parametrize(("method", "sealed"), [("GET", False), ("HEAD", False), ("POST", True), ("DELETE", True)])
def test_client_seals_an_empty_object_for_methods_that_need_a_body(client, method, sealed):
    encrypted = client.encrypt_request(method, API_PATH, None)

    assert (encrypted.body is not None) is sealed


def test_fresh_nonce_per_encryption(private_key_pem):
    cipher = HybridPayloadCipher(private_key_pem)
    key = bytes(range(32))

    first = cipher.encrypt(key, b'{"a":1}', aad="v1|res|GET|/x|1")
    second = cipher.encrypt(key, b'{"a":1}', aad="v1|res|GET|/x|1")

    assert first != second
    assert cipher.decrypt(key, first, aad="v1|res|GET|/x|1") == cipher.decrypt(key, second, aad="v1|res|GET|/x|1")


def test_aad_binds_method_and_path(private_key_pem):
    cipher = HybridPayloadCipher(private_key_pem)
    key = bytes(range(32))
    sealed = cipher.encrypt(key, b"{}", aad=payload_aad("req", "POST", API_PATH, 10))

    for aad in (
        payload_aad("req", "PUT", API_PATH, 10),
        payload_aad("req", "POST", "/hotel/v1/payments", 10),
        payload_aad("req", "POST", API_PATH, 11),
        payload_aad("res", "POST", API_PATH, 10),
    ):
        with pytest.raises(ValidationFailed) as error:
            cipher.decrypt(key, sealed, aad=aad)
        assert error.value.status_code is PAYLOAD_DECRYPTION_FAILED


@pytest.mark.parametrize("wrapped_key", ["not base64!", "QUJD"])
def test_unwrap_rejects_garbage(private_key_pem, wrapped_key):
    with pytest.raises(ValidationFailed):
        HybridPayloadCipher(private_key_pem).unwrap_key(wrapped_key)


def test_get_payload_cipher_reads_the_key_path_lazily_and_caches(encryption_settings, private_key_pem):
    cipher = get_payload_cipher()

    assert cipher is get_payload_cipher()
    assert cipher.public_key_pem() == HybridPayloadCipher(private_key_pem).public_key_pem()


# --- middleware -----------------------------------------------------------------------------------------------


def test_round_trip_request_and_response(middleware, client):
    encrypted, response = _send_encrypted(middleware, client, "POST", API_PATH, {"guest": "Rahul", "rooms": 2})

    request = middleware.seen[0]
    assert response.status_code == 200
    assert response["X-Payload-Encryption"] == "v1"
    assert request.META["CONTENT_TYPE"] == "application/json"
    assert "Rahul" not in response.content.decode()
    envelope = client.decrypt_response(encrypted, json.loads(response.content))
    assert envelope["data"] == {"method": "POST", "received": {"guest": "Rahul", "rooms": 2}}


def test_query_string_round_trip(middleware, client):
    encrypted, response = _send_encrypted(middleware, client, "GET", f"{API_PATH}?property_id=a")

    assert response.status_code == 200
    assert client.decrypt_response(encrypted, json.loads(response.content))["data"]["method"] == "GET"


def test_tampered_query_string_rejected(middleware, client):
    encrypted = client.encrypt_request("POST", f"{API_PATH}?property_id=a", {"rooms": 1})

    response = _send(
        middleware, "POST", f"{API_PATH}?property_id=b", headers=encrypted.headers, body=encrypted.body
    )

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code
    assert middleware.seen == []


def test_tampered_query_string_on_a_get_gives_a_response_the_client_refuses(middleware, client):
    encrypted = client.encrypt_request("GET", f"{API_PATH}?property_id=a", None)

    response = _send(middleware, "GET", f"{API_PATH}?property_id=b", headers=encrypted.headers)

    with pytest.raises(ValidationFailed):
        client.decrypt_response(encrypted, json.loads(response.content))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_stripped_body_rejected_for_methods_that_need_one(middleware, client, method):
    encrypted = client.encrypt_request(method, API_PATH, {"reason": "plans changed"})

    response = _send(middleware, method, API_PATH, headers=encrypted.headers)

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code
    assert middleware.seen == []


def test_post_without_a_body_is_sent_as_a_sealed_empty_object(middleware, client):
    encrypted, response = _send_encrypted(middleware, client, "POST", API_PATH)

    assert response.status_code == 200
    assert client.decrypt_response(encrypted, json.loads(response.content))["data"]["received"] == {}


def test_get_with_headers_and_empty_body_encrypts_the_response(middleware, client):
    encrypted, response = _send_encrypted(middleware, client, "GET", API_PATH)

    assert response.status_code == 200
    assert client.decrypt_response(encrypted, json.loads(response.content))["data"]["received"] is None


def test_error_responses_from_the_view_are_encrypted(encryption_settings, client):
    from hotel_commons.middlewares.api_encryption import ApiEncryptionMiddleware

    def failing_view(request: HttpRequest) -> HttpResponse:
        return JsonResponse({"status": 2100006, "message": "Booking not found"}, status=404)

    encrypted, response = _send_encrypted(ApiEncryptionMiddleware(failing_view), client, "GET", API_PATH)

    assert response.status_code == 404
    assert client.decrypt_response(encrypted, json.loads(response.content))["message"] == "Booking not found"


def test_tampered_payload_rejected(middleware, client):
    encrypted = client.encrypt_request("POST", API_PATH, {"rooms": 1})
    payload = encrypted.body["payload"]
    tampered = payload[:20] + ("A" if payload[20] != "A" else "B") + payload[21:]

    response = _send(middleware, "POST", API_PATH, headers=encrypted.headers, body={"payload": tampered})

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code
    assert middleware.seen == []


def test_payload_sent_to_another_path_rejected(middleware, client):
    encrypted = client.encrypt_request("POST", API_PATH, {"rooms": 1})

    response = _send(middleware, "POST", "/hotel/v1/payments", headers=encrypted.headers, body=encrypted.body)

    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code
    assert middleware.seen == []


def test_replayed_wrapped_key_rejected(middleware, client):
    encrypted, first = _send_encrypted(middleware, client, "POST", API_PATH, {"rooms": 1})

    replay = _send(middleware, "POST", API_PATH, headers=encrypted.headers, body=encrypted.body)

    assert first.status_code == 200
    assert replay.status_code == 400
    assert _status(replay) == PAYLOAD_DECRYPTION_FAILED.code
    assert len(middleware.seen) == 1


def test_replay_guard_outlives_the_accepted_timestamp_window(middleware, client):
    """A timestamp is accepted from ts - skew to ts + skew, so the claimed key must live at least 2 x skew."""
    encrypted, response = _send_encrypted(middleware, client, "POST", API_PATH, {"rooms": 1})
    guard_key = redis_key("enc", "v1", hashlib.sha256(encrypted.headers["X-Encrypted-Key"].encode()).hexdigest())

    assert response.status_code == 200
    assert get_redis().ttl(guard_key) > 2 * MAX_CLOCK_SKEW_SECONDS


def test_replay_guard_fails_open_when_redis_is_down(middleware, client, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    def redis_down(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(get_redis(), "set", redis_down)

    _, response = _send_encrypted(middleware, client, "POST", API_PATH, {"rooms": 1})

    assert response.status_code == 200


@pytest.mark.parametrize("skew_seconds", [-301, 301])
def test_stale_timestamp_rejected(middleware, private_key_pem, skew_seconds):
    skewed = HybridPayloadClient(_public_pem(private_key_pem), clock=lambda: time.time() + skew_seconds)

    _, response = _send_encrypted(middleware, skewed, "POST", API_PATH, {"rooms": 1})

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code


def test_overlong_timestamp_rejected_without_an_error(middleware, client):
    encrypted = client.encrypt_request("POST", API_PATH, {"rooms": 1})
    headers = {**encrypted.headers, "X-Payload-Timestamp": "9" * 5000}

    response = _send(middleware, "POST", API_PATH, headers=headers, body=encrypted.body)

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code


def test_malformed_headers_rejected(middleware, client):
    encrypted = client.encrypt_request("POST", API_PATH, {"rooms": 1})
    headers = {**encrypted.headers, "X-Payload-Timestamp": "soon"}

    response = _send(middleware, "POST", API_PATH, headers=headers, body=encrypted.body)

    assert _status(response) == PAYLOAD_DECRYPTION_FAILED.code


def test_optional_mode_passes_plaintext(middleware):
    response = _send(middleware, "POST", API_PATH, body={"rooms": 1})

    assert response.status_code == 200
    assert not response.has_header("X-Payload-Encryption")
    assert json.loads(response.content)["data"]["received"] == {"rooms": 1}


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_enforced_mode_rejects_plaintext(middleware, method):
    with override_settings(API_ENCRYPTION_MODE="enforced"):
        response = _send(middleware, method, API_PATH, body={"rooms": 1} if method == "POST" else None)

    assert response.status_code == 400
    assert _status(response) == PAYLOAD_NOT_ENCRYPTED.code
    assert middleware.seen == []


def test_enforced_mode_accepts_encrypted_requests(middleware, client):
    with override_settings(API_ENCRYPTION_MODE="enforced"):
        encrypted, response = _send_encrypted(middleware, client, "POST", API_PATH, {"rooms": 1})

    assert client.decrypt_response(encrypted, json.loads(response.content))["data"]["received"] == {"rooms": 1}


def test_bypass_path_passes_plaintext_in_enforced_mode(middleware):
    with override_settings(API_ENCRYPTION_MODE="enforced"):
        response = _send(middleware, "GET", BYPASS_PATH)

    assert response.status_code == 200
    assert not response.has_header("X-Payload-Encryption")


def test_bypass_is_an_exact_path_match(middleware):
    with override_settings(API_ENCRYPTION_MODE="enforced"):
        response = _send(middleware, "GET", BYPASS_PATH + "/extra")

    assert _status(response) == PAYLOAD_NOT_ENCRYPTED.code


def test_off_mode_ignores_headers(middleware, client):
    with override_settings(API_ENCRYPTION_MODE="off"):
        encrypted, response = _send_encrypted(middleware, client, "POST", API_PATH, {"rooms": 1})

    assert response.status_code == 200
    assert not response.has_header("X-Payload-Encryption")
    assert json.loads(response.content)["data"]["received"] == encrypted.body


def test_encrypted_requests_are_rate_limited_per_ip_before_rsa_work(encryption_settings, client, monkeypatch):
    from hotel_commons.middlewares.api_encryption import ApiEncryptionMiddleware
    from hotel_commons.ratelimit.interfaces import RateLimitDecision

    class DenyingLimiter:
        def __init__(self) -> None:
            self.subjects: list[str] = []

        def hit(self, subject, policy):
            self.subjects.append(f"{policy.scope}:{subject}")
            return RateLimitDecision(allowed=False, limit=policy.limit, remaining=0, retry_after_seconds=7)

    def unexpected_rsa_work(*args, **kwargs):
        raise AssertionError("RSA work before the rate limit")

    monkeypatch.setattr(api_crypto.HybridPayloadCipher, "unwrap_key", unexpected_rsa_work)
    limiter = DenyingLimiter()
    instance = ApiEncryptionMiddleware(lambda request: JsonResponse({}), rate_limiter=limiter)

    _, response = _send_encrypted(instance, client, "POST", API_PATH, {"rooms": 1})

    assert response.status_code == 429
    assert response["Retry-After"] == "7"
    assert _status(response) == RATE_LIMITED.code
    assert limiter.subjects == ["encrypted:ip:127.0.0.1"]


def test_encrypted_rate_limit_follows_the_encrypted_policy_setting(encryption_settings, client):
    from hotel_commons.middlewares.api_encryption import ApiEncryptionMiddleware
    from hotel_commons.ratelimit.interfaces import RateLimitDecision

    policies = []

    class RecordingLimiter:
        def hit(self, subject, policy):
            policies.append(policy)
            return RateLimitDecision(allowed=True, limit=policy.limit, remaining=1, retry_after_seconds=0)

    instance = ApiEncryptionMiddleware(lambda request: JsonResponse({}), rate_limiter=RecordingLimiter())
    tightened = {
        "default": {"limit": 120, "window_seconds": 60, "fail_open": True},
        "encrypted": {"limit": 30, "window_seconds": 60, "fail_open": False},
    }
    with override_settings(RATE_LIMIT_POLICIES=tightened):
        _send_encrypted(instance, client, "POST", API_PATH, {"rooms": 1})

    assert [(p.scope, p.limit, p.window_seconds, p.fail_open) for p in policies] == [("encrypted", 30, 60, False)]


# --- public key view ------------------------------------------------------------------------------------------


def test_public_key_view_serves_the_server_key(encryption_settings, private_key_pem):
    from rest_framework.test import APIRequestFactory

    from hotel_commons.api.encryption_views import PublicKeyView
    from hotel_commons.security.principal import ANONYMOUS

    request = APIRequestFactory().get("/hotel/v1/security/public-key")
    request.principal = ANONYMOUS

    response = PublicKeyView.as_view()(request)

    assert response.status_code == 200
    assert response.data["data"] == {
        "algorithm": ALGORITHM,
        "public_key_pem": HybridPayloadCipher(private_key_pem).public_key_pem(),
    }
