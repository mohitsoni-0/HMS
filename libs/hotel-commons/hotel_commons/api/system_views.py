"""Operational endpoints (LLD §3.9, §10.2 rows 1 and 3): the health check and local dev tokens.

``HealthView`` reports the database (``SELECT 1``) and Redis (``PING``). The database is required, so its
loss answers 503; Redis is not (the rate limiter fails open and idempotency falls back to the database's
unique keys), so its loss is reported as ``"down"`` with a 200.

``DevTokenView`` mints bearer tokens for the local demo. Its route is mounted only when
``ENVIRONMENT == "local"`` and the view refuses to work anywhere else; ADMIN tokens additionally need
``DEV_TOKENS_ALLOW_ADMIN``.
"""

import logging
from http import HTTPStatus
from typing import Final

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import DatabaseError, connection
from drf_spectacular.utils import extend_schema
from redis.exceptions import RedisError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.consts import ENVIRONMENT_LOCAL
from hotel_commons.exceptions import Forbidden, NotFound
from hotel_commons.redis.client import get_redis
from hotel_commons.security.jwt_tokens import SUBJECT_PATTERN, get_token_service
from hotel_commons.security.principal import Role
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import HotelCharField, HotelEnumField, HotelIntegerField, HotelListField
from hotel_commons.status_code import FORBIDDEN, INTERNAL_ERROR, NOT_FOUND, SUCCESS

logger = logging.getLogger(__name__)

STATUS_OK: Final = "ok"
STATUS_DOWN: Final = "down"


class HealthSerializer(BaseHotelSerializer):
    database = HotelCharField(help_text='"ok" or "down"')
    redis = HotelCharField(help_text='"ok" or "down"')


class DevTokenRequestSerializer(BaseHotelSerializer):
    user_id = HotelCharField(max_length=64, validators=[RegexValidator(f"^{SUBJECT_PATTERN.pattern}$")])
    roles = HotelListField(child=HotelEnumField(Role), min_length=1)


class DevTokenSerializer(BaseHotelSerializer):
    token = HotelCharField()
    expires_in = HotelIntegerField(help_text="Seconds until the token expires.")


HEALTH_ENVELOPE = envelope(HealthSerializer, "HealthEnvelope")


class HealthView(APIView):
    permission_classes = [AllowAny]
    throttle_classes: list = []

    @extend_schema(responses={200: HEALTH_ENVELOPE, 503: HEALTH_ENVELOPE})
    def get(self, request: Request) -> Response:
        data = {"database": _database_status(), "redis": _redis_status()}
        if data["database"] != STATUS_OK:
            return api_response(data, http_status=HTTPStatus.SERVICE_UNAVAILABLE, status=INTERNAL_ERROR)
        return api_response(data, status=SUCCESS)


class DevTokenView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        request=DevTokenRequestSerializer,
        responses={200: envelope(DevTokenSerializer, "DevTokenEnvelope"), 400: ERROR_ENVELOPE, 403: ERROR_ENVELOPE},
    )
    def post(self, request: Request) -> Response:
        if settings.ENVIRONMENT.lower() != ENVIRONMENT_LOCAL:
            raise NotFound(NOT_FOUND)
        serializer = DevTokenRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        roles = frozenset(serializer.validated_data["roles"])
        if Role.ADMIN in roles and not settings.DEV_TOKENS_ALLOW_ADMIN:
            raise Forbidden(FORBIDDEN)
        service = get_token_service()
        token = service.issue(user_id=serializer.validated_data["user_id"], roles=roles)
        return api_response(DevTokenSerializer({"token": token, "expires_in": service.ttl_seconds}).data)


def _database_status() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.warning("Health check: database unreachable")
        return STATUS_DOWN
    return STATUS_OK


def _redis_status() -> str:
    try:
        get_redis().ping()
    except RedisError:
        logger.warning("Health check: Redis unreachable")
        return STATUS_DOWN
    return STATUS_OK
