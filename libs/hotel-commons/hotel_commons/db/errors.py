"""Database errors a client should retry, mapped to the error catalogue (LLD §3.15, HLD §8).

The connection runs with ``lock_timeout`` and ``statement_timeout`` (base settings), so a request stuck
behind a row lock fails fast instead of queueing. Django raises those as ``OperationalError`` with the
psycopg error as ``__cause__``; the SQLSTATE says which limit fired:

- 55P03 lock_not_available -> 409 RESOURCE_BUSY: another request holds the row, retry shortly;
- 57014 query_canceled (statement timeout) -> 503 SERVICE_TIMEOUT: the database is slow, retry shortly.

Both carry ``Retry-After: 1``. Any other database error is a real failure and stays a 500.
"""

from collections.abc import Mapping
from typing import Final

from django.db import DatabaseError

from hotel_commons.exceptions import RetryLater
from hotel_commons.status_code import RESOURCE_BUSY, SERVICE_TIMEOUT, StatusCode

LOCK_NOT_AVAILABLE: Final = "55P03"
QUERY_CANCELED: Final = "57014"
RETRY_AFTER_SECONDS: Final = 1

_RETRYABLE: Final[Mapping[str, StatusCode]] = {LOCK_NOT_AVAILABLE: RESOURCE_BUSY, QUERY_CANCELED: SERVICE_TIMEOUT}


def retryable_database_error(exc: BaseException) -> RetryLater | None:
    """The ``RetryLater`` for a lock or statement timeout behind ``exc``, or None for anything else."""
    if not isinstance(exc, DatabaseError):
        return None
    status = _RETRYABLE.get(_sqlstate(exc) or "")
    return RetryLater(status, retry_after_seconds=RETRY_AFTER_SECONDS) if status is not None else None


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of the driver error that ``exc`` wraps (Django chains it as ``__cause__``)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None)
        if isinstance(sqlstate, str):
            return sqlstate
        current = current.__cause__
    return None
