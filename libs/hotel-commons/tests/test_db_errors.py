"""Lock and statement timeouts answer 409 RESOURCE_BUSY / 503 SERVICE_TIMEOUT with Retry-After, never a 500
(review D3/B1, LLD §3.15, HLD §8).

The unit tests build Django's ``OperationalError`` the way Django raises it, with the psycopg error as its
cause. The database tests hold a real row lock from a second connection, and run a real slow statement.
"""

import logging

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, OperationalError, connection
from django.http import HttpResponse
from django.test import RequestFactory
from psycopg.errors import LockNotAvailable, QueryCanceled, SerializationFailure
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from hotel_commons.api.exception_handler import envelope_exception_handler
from hotel_commons.db.errors import retryable_database_error
from hotel_commons.exceptions import RetryLater, TooManyRequests
from hotel_commons.middlewares.exception_handling import ExceptionHandlingMiddleware
from hotel_commons.status_code import INTERNAL_ERROR, RATE_LIMITED, RESOURCE_BUSY, SERVICE_TIMEOUT


def _django_error(cause: Exception, wrapper: type[DatabaseError] = OperationalError) -> DatabaseError:
    error = wrapper(str(cause))
    error.__cause__ = cause
    return error


LOCK_TIMEOUT = _django_error(LockNotAvailable("canceling statement due to lock timeout"))
STATEMENT_TIMEOUT = _django_error(QueryCanceled("canceling statement due to statement timeout"))


@pytest.mark.parametrize(
    ("exc", "status"),
    [(LOCK_TIMEOUT, RESOURCE_BUSY), (STATEMENT_TIMEOUT, SERVICE_TIMEOUT)],
)
def test_timeouts_map_to_retryable_errors(exc, status):
    mapped = retryable_database_error(exc)
    assert isinstance(mapped, RetryLater)
    assert (mapped.status_code, mapped.http_status, mapped.retry_after_seconds) == (status, status.http_status, 1)


@pytest.mark.parametrize(
    "exc",
    [
        _django_error(SerializationFailure("could not serialize access")),
        _django_error(psycopg.errors.UniqueViolation("duplicate key"), IntegrityError),
        OperationalError("server closed the connection unexpectedly"),  # no driver cause
        RuntimeError("not a database error"),
    ],
)
def test_other_errors_are_not_retryable(exc):
    assert retryable_database_error(exc) is None


def test_status_codes_match_the_contract():
    assert (RESOURCE_BUSY.code, RESOURCE_BUSY.http_status) == (2100019, 409)
    assert (SERVICE_TIMEOUT.code, SERVICE_TIMEOUT.http_status) == (2100020, 503)


@pytest.mark.parametrize(
    ("exc", "status"),
    [(LOCK_TIMEOUT, RESOURCE_BUSY), (STATEMENT_TIMEOUT, SERVICE_TIMEOUT)],
)
def test_drf_handler_answers_the_envelope_with_retry_after(exc, status, caplog):
    with caplog.at_level(logging.WARNING):
        response = envelope_exception_handler(exc, {})

    assert response is not None
    assert (response.status_code, response.data["status"]) == (status.http_status, status.code)
    assert response.data["message"] == status.message
    assert response["Retry-After"] == "1"
    assert [record.levelno for record in caplog.records] == [logging.WARNING]  # expected under load, not a bug


def test_rate_limits_are_still_not_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger="hotel_commons"):
        response = envelope_exception_handler(TooManyRequests(RATE_LIMITED, retry_after_seconds=3), {})
    assert (response.status_code, response["Retry-After"]) == (429, "3")
    assert caplog.records == []


def test_drf_handler_leaves_other_database_errors_to_the_middleware():
    assert envelope_exception_handler(OperationalError("connection lost"), {}) is None


@pytest.mark.parametrize(
    ("exc", "status"),
    [(LOCK_TIMEOUT, RESOURCE_BUSY), (STATEMENT_TIMEOUT, SERVICE_TIMEOUT)],
)
def test_middleware_answers_the_envelope_with_retry_after(exc, status, caplog):
    middleware = ExceptionHandlingMiddleware(lambda request: HttpResponse())
    request = RequestFactory().post("/hotel/v1/bookings/1/cancel")

    with caplog.at_level(logging.WARNING):
        response = middleware.process_exception(request, exc)

    assert response.status_code == status.http_status
    assert response["Retry-After"] == "1"
    assert b'"status": %d' % status.code in response.content
    assert all(record.levelno == logging.WARNING for record in caplog.records)


def test_middleware_still_answers_500_for_other_database_errors():
    middleware = ExceptionHandlingMiddleware(lambda request: HttpResponse())
    response = middleware.process_exception(RequestFactory().get("/x"), OperationalError("connection lost"))
    assert response.status_code == INTERNAL_ERROR.http_status
    assert not response.has_header("Retry-After")


# --- Against PostgreSQL ------------------------------------------------------------------------------------


class LockingView(APIView):
    """Locks the ``django_content_type`` row ``row_id`` like a use case does, under a short lock timeout."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []
    row_id = 0

    def post(self, request):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '200ms'")
            cursor.execute("SELECT id FROM django_content_type WHERE id = %s FOR UPDATE", [self.row_id])
        return Response({"locked": True})


class SlowView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def post(self, request):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '50ms'")
            cursor.execute("SELECT pg_sleep(2)")
        return Response({"slept": True})


@pytest.mark.db
@pytest.mark.django_db
def test_a_row_locked_by_another_transaction_answers_409_resource_busy():
    with connection.cursor() as cursor:
        cursor.execute("SELECT min(id) FROM django_content_type")
        LockingView.row_id = cursor.fetchone()[0]
    holder = _second_connection()
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT id FROM django_content_type WHERE id = %s FOR UPDATE", [LockingView.row_id])

        response = LockingView.as_view()(APIRequestFactory().post("/locked", {}, format="json"))
    finally:
        holder.rollback()
        holder.close()

    assert (response.status_code, response.data["status"]) == (409, RESOURCE_BUSY.code)
    assert response["Retry-After"] == "1"


@pytest.mark.db
@pytest.mark.django_db
def test_a_statement_timeout_answers_503_service_timeout():
    response = SlowView.as_view()(APIRequestFactory().post("/slow", {}, format="json"))

    assert (response.status_code, response.data["status"]) == (503, SERVICE_TIMEOUT.code)
    assert response["Retry-After"] == "1"


def _second_connection() -> psycopg.Connection:
    """A connection of its own to the test database, as another request's would be."""
    params = connection.settings_dict
    return psycopg.connect(
        host=params["HOST"], port=params["PORT"], dbname=params["NAME"], user=params["USER"],
        password=params["PASSWORD"], autocommit=True,
    )
