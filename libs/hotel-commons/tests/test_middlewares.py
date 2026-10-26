"""The request pipeline (LLD §3.8-§3.10, §3.13): middlewares, DRF authentication, BaseViewSet ordering,
throttling, the system views, the commons URLs and ``mint_token``.

No database: the health check's connection is replaced by a stub. Without the project settings
(``-p no:django``) a minimal Django is configured here; either way every setting the pipeline reads is
overridden per test, so results do not depend on which settings loaded first.
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

import importlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.db import DatabaseError  # noqa: E402
from django.http import Http404, HttpResponse  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402
from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: E402
from django.urls import path  # noqa: E402
from drf_spectacular.generators import SchemaGenerator  # noqa: E402
from drf_spectacular.utils import extend_schema  # noqa: E402
from rest_framework import serializers  # noqa: E402
from rest_framework.exceptions import ValidationError as DrfValidationError  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from hotel_commons.api import system_views, throttling  # noqa: E402
from hotel_commons.api.authentication import PrincipalAuthentication  # noqa: E402
from hotel_commons.api.error_views import not_found, server_error  # noqa: E402
from hotel_commons.api.exception_handler import envelope_exception_handler  # noqa: E402
from hotel_commons.api.response import flatten_errors  # noqa: E402
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope, envelope_list  # noqa: E402
from hotel_commons.api.throttling import RedisRateLimitThrottle  # noqa: E402
from hotel_commons.api.views import BaseViewSet  # noqa: E402
from hotel_commons.context import RequestContext  # noqa: E402
from hotel_commons.exceptions import NotFound, TooManyRequests, ValidationFailed  # noqa: E402
from hotel_commons.management.commands.mint_token import Command as MintTokenCommand  # noqa: E402
from hotel_commons.middlewares.authentication import JWTAuthenticationMiddleware  # noqa: E402
from hotel_commons.middlewares.exception_handling import ExceptionHandlingMiddleware  # noqa: E402
from hotel_commons.middlewares.request_context import RequestContextMiddleware  # noqa: E402
from hotel_commons.ratelimit.interfaces import RateLimitDecision, RateLimitPolicy  # noqa: E402
from hotel_commons.ratelimit.redis_sliding_window import get_rate_limiter  # noqa: E402
from hotel_commons.redis.client import get_redis  # noqa: E402
from hotel_commons.security.idor import IdorRule, Ownership, OwnershipResolver, ownership_registry  # noqa: E402
from hotel_commons.security.jwt_tokens import get_token_service  # noqa: E402
from hotel_commons.security.permissions import IsAuthenticatedPrincipal, require_roles  # noqa: E402
from hotel_commons.security.principal import ANONYMOUS, Principal, Role  # noqa: E402
from hotel_commons.status_code import (  # noqa: E402
    FORBIDDEN,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    RATE_LIMITED,
    RESOURCE_NOT_FOUND,
    TOKEN_EXPIRED,
    UNAUTHENTICATED,
    VALIDATION_FAILED,
)

PIPELINE_SETTINGS = {
    "ENVIRONMENT": "local",
    "REDIS_URL": "fakeredis://",
    "JWT_SIGNING_KEY": b"j" * 32,
    "JWT_ISSUER": "hotel-booking",
    "JWT_AUDIENCE": "hotel-booking-api",
    "JWT_TTL_SECONDS": 3600,
    "JWT_LEEWAY_SECONDS": 30,
    "RATE_LIMIT_ENABLED": True,
    "RATE_LIMIT_POLICIES": {
        "default": {"limit": 2, "window_seconds": 60, "fail_open": True},
        "search": {"limit": 60, "window_seconds": 60, "fail_open": True},
        "payment": {"limit": 10, "window_seconds": 60, "fail_open": False},
    },
    "TRUSTED_PROXY_COUNT": 0,
    "DEV_TOKENS_ALLOW_ADMIN": False,
    "REST_FRAMEWORK": {
        "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
        "DEFAULT_AUTHENTICATION_CLASSES": ["hotel_commons.api.authentication.PrincipalAuthentication"],
        "DEFAULT_PERMISSION_CLASSES": ["hotel_commons.security.permissions.IsAuthenticatedPrincipal"],
        "DEFAULT_THROTTLE_CLASSES": ["hotel_commons.api.throttling.RedisRateLimitThrottle"],
        "EXCEPTION_HANDLER": "hotel_commons.api.exception_handler.envelope_exception_handler",
        "UNAUTHENTICATED_USER": None,
    },
}
GUEST = Principal(user_id="guest-1", roles=frozenset({Role.GUEST}))
ADMIN = Principal(user_id="admin-1", roles=frozenset({Role.ADMIN}))


@pytest.fixture(autouse=True)
def pipeline_settings():
    with override_settings(**PIPELINE_SETTINGS):
        _clear_caches()
        get_redis().flushall()  # always the in-process FakeRedis here: REDIS_URL is overridden above
        yield
    _clear_caches()


def _clear_caches() -> None:
    for cached in (get_redis, get_token_service, get_rate_limiter):
        cached.cache_clear()


class RecordingLimiter:
    """Records (scope, subject) per hit and answers ``allowed``."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.hits: list[tuple[str, str]] = []

    def hit(self, subject: str, policy: RateLimitPolicy) -> RateLimitDecision:
        self.hits.append((policy.scope, subject))
        remaining = policy.limit - len(self.hits) if self.allowed else 0
        return RateLimitDecision(allowed=self.allowed, limit=policy.limit, remaining=remaining,
                                 retry_after_seconds=0 if self.allowed else 7)


@pytest.fixture
def limiter(monkeypatch) -> RecordingLimiter:
    recording = RecordingLimiter()
    monkeypatch.setattr(throttling, "get_rate_limiter", lambda: recording)
    return recording


def _body(response) -> dict:
    return json.loads(response.content)


def _token(principal: Principal) -> str:
    return get_token_service().issue(user_id=principal.user_id, roles=principal.roles)


# --- RequestContextMiddleware ------------------------------------------------------------------------------


def test_request_context_echoes_a_valid_request_id_and_binds_it():
    seen = {}

    def view(request):
        seen.update(request_id=request.request_id, bound=RequestContext.request_id(),
                    principal=RequestContext.principal())
        return HttpResponse()

    response = RequestContextMiddleware(view)(RequestFactory().get("/", HTTP_X_REQUEST_ID="req-123-abc"))

    assert response["X-Request-ID"] == "req-123-abc"
    assert seen == {"request_id": "req-123-abc", "bound": "req-123-abc", "principal": ANONYMOUS}
    assert RequestContext.request_id() is None


@pytest.mark.parametrize("header", [None, "x" * 65, "bad id", "semi;colon", ""])
def test_request_context_replaces_a_missing_or_unsafe_request_id(header):
    extra = {"HTTP_X_REQUEST_ID": header} if header is not None else {}

    response = RequestContextMiddleware(lambda request: HttpResponse())(RequestFactory().get("/", **extra))

    assert re.fullmatch(r"[0-9a-f]{32}", response["X-Request-ID"])


def test_request_context_resets_even_when_the_view_raises():
    def view(request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        RequestContextMiddleware(view)(RequestFactory().get("/"))

    assert RequestContext.request_id() is None


# --- ExceptionHandlingMiddleware ---------------------------------------------------------------------------


def _handled(exception: Exception):
    middleware = ExceptionHandlingMiddleware(lambda request: HttpResponse())
    return middleware.process_exception(RequestFactory().get("/hotel/v1/bookings"), exception)


def test_app_error_becomes_its_json_envelope():
    tokens = RequestContext.bind(request_id="req-1")
    try:
        response = _handled(NotFound(RESOURCE_NOT_FOUND, resource="booking"))
    finally:
        RequestContext.reset(tokens)

    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert _body(response) == {"status": RESOURCE_NOT_FOUND.code, "message": "booking not found",
                               "errors": None, "data": None, "request_id": "req-1"}


def test_domain_field_errors_become_lists_of_messages():
    """A domain ``ValidationFailed`` maps a field to one string; the envelope always carries a list (A5)."""
    response = _handled(ValidationFailed(VALIDATION_FAILED, errors={"card.card_number": "is invalid"}))

    assert _body(response)["errors"] == {"card.card_number": ["is invalid"]}


def test_drf_validation_errors_are_keyed_by_dotted_path():
    detail = {
        "card": {"card_number": ["Enter a valid card number."], "cvv": ["This field is required."]},
        "room_types": [{}, {"base_price": ["Must be at least 1.00."]}],
        "amenities": {2: ["Unknown code."]},
        "non_field_errors": ["Send card details, and only those."],
    }

    response = envelope_exception_handler(DrfValidationError(detail), {})

    assert response.data["errors"] == {
        "card.card_number": ["Enter a valid card number."],
        "card.cvv": ["This field is required."],
        "room_types.1.base_price": ["Must be at least 1.00."],
        "amenities.2": ["Unknown code."],
        "non_field_errors": ["Send card details, and only those."],
    }
    assert all(type(message) is str for messages in response.data["errors"].values() for message in messages)


def test_flatten_errors_keeps_every_message():
    assert flatten_errors({"a": ["x", {"b": "y"}], "c": None, "d": 3}) == {"a": ["x"], "a.1.b": ["y"], "d": ["3"]}


def test_too_many_requests_carries_retry_after():
    response = _handled(TooManyRequests(RATE_LIMITED, retry_after_seconds=9))

    assert (response.status_code, response["Retry-After"]) == (429, "9")


def test_unexpected_exception_becomes_a_500_envelope(caplog):
    response = _handled(RuntimeError("secret detail"))

    assert response.status_code == 500
    assert _body(response)["status"] == INTERNAL_ERROR.code
    assert "secret detail" not in response.content.decode()
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_django_http404_is_left_to_the_404_handler():
    assert _handled(Http404()) is None


def test_middleware_passes_responses_through():
    response = ExceptionHandlingMiddleware(lambda request: HttpResponse(status=204))(RequestFactory().get("/"))

    assert response.status_code == 204


# --- JWTAuthenticationMiddleware ---------------------------------------------------------------------------


def _authenticate(authorization: str | None):
    seen = {}

    def view(request):
        seen.update(principal=request.principal, bound=RequestContext.principal())
        return HttpResponse()

    extra = {"HTTP_AUTHORIZATION": authorization} if authorization is not None else {}
    response = JWTAuthenticationMiddleware(view)(RequestFactory().get("/", **extra))
    return response, seen


def test_no_authorization_header_is_anonymous():
    response, seen = _authenticate(None)

    assert response.status_code == 200
    assert seen == {"principal": ANONYMOUS, "bound": ANONYMOUS}


def test_valid_bearer_token_binds_the_principal_for_the_request():
    response, seen = _authenticate(f"Bearer {_token(GUEST)}")

    assert response.status_code == 200
    assert seen == {"principal": GUEST, "bound": GUEST}
    assert RequestContext.principal() == ANONYMOUS


@pytest.mark.parametrize("authorization", ["Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "])
def test_bad_credentials_get_a_401_envelope_without_reaching_the_view(authorization):
    response, seen = _authenticate(authorization)

    assert response.status_code == 401
    assert _body(response)["status"] == UNAUTHENTICATED.code
    assert response["WWW-Authenticate"] == "Bearer"
    assert seen == {}


def test_expired_token_gets_token_expired():
    expired = get_token_service().issue(user_id="guest-1", roles=[Role.GUEST], ttl_seconds=-120)

    response, _ = _authenticate(f"Bearer {expired}")

    assert (response.status_code, _body(response)["status"]) == (401, TOKEN_EXPIRED.code)


# --- PrincipalAuthentication and permissions ---------------------------------------------------------------


class WhoAmIView(APIView):
    authentication_classes = [PrincipalAuthentication]
    permission_classes = [IsAuthenticatedPrincipal]
    throttle_classes: list = []

    def get(self, request):
        return Response({"user_id": request.user.user_id})


def _api_get(view, principal: Principal = ANONYMOUS, path: str = "/hotel/v1/whoami", **kwargs):
    request = APIRequestFactory().get(path)
    request.principal = principal
    return view(request, **kwargs)


def test_principal_authentication_exposes_the_middleware_principal():
    response = _api_get(WhoAmIView.as_view(), GUEST)

    assert (response.status_code, response.data) == (200, {"user_id": "guest-1"})


def test_anonymous_gets_401_with_bearer_challenge():
    response = _api_get(WhoAmIView.as_view())

    assert (response.status_code, response.data["status"]) == (401, UNAUTHENTICATED.code)
    assert response["WWW-Authenticate"] == "Bearer"


def test_require_roles_is_a_cached_permission_class():
    assert require_roles(Role.GUEST, Role.ADMIN) is require_roles(Role.ADMIN, Role.GUEST)
    assert require_roles(Role.GUEST) is not require_roles(Role.ADMIN)
    with pytest.raises(ValueError):
        require_roles()


# --- BaseViewSet: authenticate -> throttles -> permissions -> IDOR -----------------------------------------

TEST_RESOURCE = "test_pipeline_booking"
OWNED_BOOKING = uuid4()


class _OwnedBookings(OwnershipResolver):
    def resolve(self, resource_id: UUID) -> Ownership | None:
        if resource_id == OWNED_BOOKING:
            return Ownership(guest_user_ids=frozenset({GUEST.user_id}))
        return None


ownership_registry.register(TEST_RESOURCE, _OwnedBookings())


class BookingsViewSet(BaseViewSet):
    permission_classes_by_action = {"retrieve": [require_roles(Role.GUEST, Role.ADMIN)],
                                    "manage": [require_roles(Role.ADMIN)]}
    throttle_scope_by_action = {"manage": "payment"}
    idor_rules = {"retrieve": [IdorRule(resource=TEST_RESOURCE, key="booking_id", relations=frozenset({"guest"}))]}

    def retrieve(self, request, booking_id):
        return Response({"booking_id": str(booking_id)})

    def manage(self, request):
        return Response({"managed": True})

    def overview(self, request):
        return Response({"overview": True})


def _viewset_get(actions: dict[str, str], principal: Principal = ANONYMOUS, **kwargs):
    return _api_get(BookingsViewSet.as_view(actions), principal, **kwargs)


def test_throttles_run_before_permissions(limiter):
    limiter.allowed = False

    forbidden_role = _viewset_get({"get": "manage"}, GUEST)
    anonymous = _viewset_get({"get": "manage"})

    assert (forbidden_role.status_code, forbidden_role.data["status"]) == (429, RATE_LIMITED.code)
    assert forbidden_role["Retry-After"] == "7"
    assert anonymous.status_code == 429
    assert limiter.hits == [("payment", "u:guest-1"), ("payment", "ip:127.0.0.1")]


def test_permissions_per_action(limiter):
    assert _viewset_get({"get": "manage"}, GUEST).data["status"] == FORBIDDEN.code
    assert _viewset_get({"get": "manage"}, ADMIN).status_code == 200


def test_actions_without_an_entry_use_the_default_permission(limiter):
    assert _viewset_get({"get": "overview"}).status_code == 401
    assert _viewset_get({"get": "overview"}, GUEST).status_code == 200


def test_idor_runs_after_permissions_and_hides_foreign_ids(limiter):
    own = _viewset_get({"get": "retrieve"}, GUEST, booking_id=OWNED_BOOKING)
    foreign = _viewset_get({"get": "retrieve"}, GUEST, booking_id=uuid4())

    assert own.status_code == 200
    assert (foreign.status_code, foreign.data["status"]) == (404, RESOURCE_NOT_FOUND.code)


def test_throttle_scope_defaults_to_default(limiter):
    _viewset_get({"get": "overview"}, GUEST)

    assert limiter.hits == [("default", "u:guest-1")]


def test_options_is_a_405_envelope_not_a_500(limiter):
    """DRF's metadata would build a serializer for the POST action of a viewset that has none (review S7)."""
    request = APIRequestFactory().options("/hotel/v1/bookings")
    request.principal = GUEST

    response = BookingsViewSet.as_view({"get": "overview", "post": "manage"})(request)

    assert (response.status_code, response.data["status"]) == (405, METHOD_NOT_ALLOWED.code)
    assert response.data["message"] == "Method OPTIONS not allowed"


def test_rate_limit_headers_follow_the_decision():
    first = _viewset_get({"get": "overview"}, GUEST)
    second = _viewset_get({"get": "overview"}, GUEST)
    third = _viewset_get({"get": "overview"}, GUEST)

    assert (first["X-RateLimit-Limit"], first["X-RateLimit-Remaining"]) == ("2", "1")
    assert second["X-RateLimit-Remaining"] == "0"
    assert (third.status_code, third["X-RateLimit-Remaining"]) == (429, "0")
    assert 1 <= int(third["Retry-After"]) <= 60


# --- RedisRateLimitThrottle on plain views -----------------------------------------------------------------


class SearchView(APIView):
    authentication_classes = [PrincipalAuthentication]
    permission_classes: list = []
    throttle_classes = [RedisRateLimitThrottle]
    throttle_scope = "search"

    def get(self, request):
        return Response({})


class PlainView(SearchView):
    throttle_scope = None


class UnknownScopeView(SearchView):
    throttle_scope = "no_such_scope"


def test_plain_views_use_their_throttle_scope_or_default(limiter):
    for view in (SearchView, PlainView, UnknownScopeView):
        _api_get(view.as_view(), GUEST)

    assert limiter.hits == [("search", "u:guest-1"), ("default", "u:guest-1"), ("default", "u:guest-1")]


def test_rate_limiting_can_be_switched_off(limiter):
    limiter.allowed = False
    with override_settings(RATE_LIMIT_ENABLED=False):
        response = _api_get(SearchView.as_view(), GUEST)

    assert response.status_code == 200 and limiter.hits == []


# --- Error views -------------------------------------------------------------------------------------------


def test_error_views_answer_with_envelopes():
    request = RequestFactory().get("/nowhere")

    missing, failed = not_found(request, Http404()), server_error(request)

    assert (missing.status_code, _body(missing)["status"]) == (404, NOT_FOUND.code)
    assert (failed.status_code, _body(failed)["status"]) == (500, INTERNAL_ERROR.code)


# --- System views ------------------------------------------------------------------------------------------


class _Cursor:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def execute(self, sql: str) -> None:
        if self.fail:
            raise DatabaseError("database is down")
        self.executed.append(sql)

    def fetchone(self) -> tuple[int]:
        return (1,)


class _Connection:
    def __init__(self, fail: bool = False) -> None:
        self.cursor_instance = _Cursor(fail)

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def test_health_reports_database_and_redis(monkeypatch):
    database = _Connection()
    monkeypatch.setattr(system_views, "connection", database)

    response = _api_get(system_views.HealthView.as_view(), path="/hotel/v1/health")

    assert (response.status_code, response.data["data"]) == (200, {"database": "ok", "redis": "ok"})
    assert database.cursor_instance.executed == ["SELECT 1"]


def test_health_reports_redis_down_without_failing(monkeypatch):
    def down() -> bool:
        raise RedisConnectionError("down")

    monkeypatch.setattr(system_views, "connection", _Connection())
    monkeypatch.setattr(get_redis(), "ping", down)

    response = _api_get(system_views.HealthView.as_view(), path="/hotel/v1/health")

    assert (response.status_code, response.data["data"]["redis"]) == (200, "down")


def test_health_is_503_when_the_database_is_down(monkeypatch):
    monkeypatch.setattr(system_views, "connection", _Connection(fail=True))

    response = _api_get(system_views.HealthView.as_view(), path="/hotel/v1/health")

    assert (response.status_code, response.data["data"]["database"]) == (503, "down")


def _post_dev_token(body: dict):
    request = APIRequestFactory().post("/hotel/v1/dev/tokens", body, format="json")
    request.principal = ANONYMOUS
    return system_views.DevTokenView.as_view()(request)


def test_dev_token_is_issued_for_guest_roles(limiter):
    response = _post_dev_token({"user_id": "guest-1", "roles": ["GUEST", "OWNER"]})

    assert response.status_code == 200
    assert response.data["data"]["expires_in"] == 3600
    principal = get_token_service().verify(response.data["data"]["token"])
    assert principal == Principal(user_id="guest-1", roles=frozenset({Role.GUEST, Role.OWNER}))


def test_dev_token_refuses_admin_unless_allowed(limiter):
    refused = _post_dev_token({"user_id": "admin-1", "roles": ["ADMIN"]})
    with override_settings(DEV_TOKENS_ALLOW_ADMIN=True):
        allowed = _post_dev_token({"user_id": "admin-1", "roles": ["ADMIN"]})

    assert (refused.status_code, refused.data["status"]) == (403, FORBIDDEN.code)
    assert allowed.status_code == 200


@pytest.mark.parametrize("body", [{"user_id": "bad id", "roles": ["GUEST"]}, {"user_id": "guest-1", "roles": []},
                                  {"user_id": "guest-1", "roles": ["ROOT"]}, {"roles": ["GUEST"]}])
def test_dev_token_validates_its_request(limiter, body):
    response = _post_dev_token(body)

    assert (response.status_code, response.data["status"]) == (400, VALIDATION_FAILED.code)


def test_dev_token_is_not_found_outside_local(limiter):
    with override_settings(ENVIRONMENT="production"):
        response = _post_dev_token({"user_id": "guest-1", "roles": ["GUEST"]})

    assert response.status_code == 404


# --- Commons URLs ------------------------------------------------------------------------------------------


@pytest.fixture
def route_names():
    """Reload the commons URLconf under an ENVIRONMENT; afterwards reload it under the real settings again."""
    module = importlib.import_module("hotel_commons.api.urls")

    def reloaded(environment: str) -> set[str]:
        with override_settings(ENVIRONMENT=environment):
            importlib.reload(module)
        return {str(pattern.pattern) for pattern in module.urlpatterns}

    yield reloaded
    importlib.reload(module)


def test_dev_tokens_route_exists_only_locally(route_names):
    assert route_names("local") == {"health", "security/public-key", "dev/tokens", "schema", "docs"}
    assert route_names("Production") == {"health", "security/public-key", "schema", "docs"}


# --- mint_token --------------------------------------------------------------------------------------------


def test_mint_token_prints_a_verifiable_token():
    out = io.StringIO()

    call_command(MintTokenCommand(), "--sub", "guest-1", "--roles", "GUEST,owner", "--ttl", "60", stdout=out)

    principal = get_token_service().verify(out.getvalue().strip())
    assert principal == Principal(user_id="guest-1", roles=frozenset({Role.GUEST, Role.OWNER}))


@pytest.mark.parametrize(
    "arguments", [["--sub", "guest-1", "--roles", "ROOT"], ["--sub", "bad id", "--roles", "GUEST"]]
)
def test_mint_token_rejects_bad_input(arguments):
    with pytest.raises(CommandError):
        call_command(MintTokenCommand(), *arguments, stdout=io.StringIO())


# --- OpenAPI envelopes (hotel_commons.api.schema) ----------------------------------------------------------


class _ThingSerializer(serializers.Serializer):
    name = serializers.CharField()


class ThingsViewSet(BaseViewSet):
    @extend_schema(responses={200: envelope_list(_ThingSerializer, "ThingPageEnvelope", paginated=True),
                              400: ERROR_ENVELOPE})
    def list(self, request):
        return Response({})

    @extend_schema(responses={200: envelope_list(_ThingSerializer, "ThingListEnvelope")})
    def names(self, request):
        return Response({})

    @extend_schema(responses={200: envelope(_ThingSerializer(many=True), "LegacyThingListEnvelope")})
    def legacy(self, request):
        return Response({})


def _schema_of(actions: dict[str, str], route: str) -> tuple[dict, dict]:
    generator = SchemaGenerator(patterns=[path(route, ThingsViewSet.as_view(actions))])
    document = generator.get_schema(request=None, public=True)
    return document, document["paths"][f"/{route}"]["get"]["responses"]


def test_a_list_action_documents_one_envelope_object_not_an_array():
    document, responses = _schema_of({"get": "list"}, "things")

    ok = responses["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ThingPageEnvelope"}  # was {"type": "array", "items": ...}
    page = document["components"]["schemas"]["ThingPageData"]
    assert set(page["properties"]) == {"items", "page", "page_size", "total"}
    assert page["properties"]["items"]["type"] == "array"
    assert responses["400"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}


def test_envelope_list_without_pagination_holds_an_array_of_items():
    document, responses = _schema_of({"get": "names"}, "names")

    ok = responses["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ThingListEnvelope"}
    data = document["components"]["schemas"]["ThingListEnvelope"]["properties"]["data"]
    assert data["type"] == "array"
    assert data["items"] == {"$ref": "#/components/schemas/_Thing"}


def test_plain_envelope_on_a_list_action_is_not_wrapped_either():
    class LegacyViewSet(ThingsViewSet):
        list = ThingsViewSet.legacy  # documented with envelope(X(many=True), ...), as list views used to be

    generator = SchemaGenerator(patterns=[path("legacy", LegacyViewSet.as_view({"get": "list"}))])
    document = generator.get_schema(request=None, public=True)

    ok = document["paths"]["/legacy"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/LegacyThingListEnvelope"}


def test_error_envelope_documents_dotted_paths_to_message_lists():
    document, _ = _schema_of({"get": "list"}, "things")

    errors = document["components"]["schemas"]["ErrorEnvelope"]["properties"]["errors"]
    assert errors["additionalProperties"] == {"type": "array", "items": {"type": "string"}}
    assert errors["nullable"] is True
