"""Application error hierarchy (LLD §3.2). Pure Python: no Django imports.

Domain code and services raise these; the API layer turns them into the error envelope with the HTTP status
of the ``StatusCode``. The subclasses are readable aliases that tell a reader what kind of failure it is,
never a second source of HTTP status.
"""

import math
from collections.abc import Mapping
from typing import Any

from hotel_commons.status_code import StatusCode


class AppError(Exception):
    """An expected failure: a StatusCode, the params that render its message, and optional field errors.

    Params end up in a message the client sees, so they name things (fields, resources, states), never
    values such as PII or card data.
    """

    def __init__(self, status_code: StatusCode, /, *, errors: Mapping[str, Any] | None = None, **params: Any) -> None:
        self.status_code = status_code
        self.params = params
        self.errors = dict(errors) if errors is not None else None
        self.message = status_code.render(**params)
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return self.status_code.http_status

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.status_code.code}, message={self.message!r})"


class ValidationFailed(AppError):
    """Input, or a value object built from it, breaks a rule (typically 400)."""


class Unauthenticated(AppError):
    """Missing, invalid or expired credentials (401)."""


class PaymentDeclined(AppError):
    """The payment method was refused (402)."""


class Forbidden(AppError):
    """The caller's role does not allow the action or field (403)."""


class NotFound(AppError):
    """The resource does not exist or is not the caller's (404, so existence is not revealed)."""


class Conflict(AppError):
    """The current state forbids the action: invalid transition, no inventory, request in progress (409)."""


class BusinessRuleViolation(AppError):
    """A well-formed request that a business rule rejects (422)."""


class TooManyRequests(AppError):
    """Rate limit hit (429). ``retry_after_seconds`` feeds the Retry-After header and the message.

    The wait is rounded up to whole seconds (HTTP delta-seconds), so a client never retries too early.
    """

    def __init__(
        self,
        status_code: StatusCode,
        /,
        *,
        retry_after_seconds: float,
        errors: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> None:
        self.retry_after_seconds = max(0, math.ceil(retry_after_seconds))
        params.setdefault("retry_after", self.retry_after_seconds)
        super().__init__(status_code, errors=errors, **params)


class SecurityFailure(AppError):
    """A security control failed closed, e.g. tampered ciphertext or a misconfigured log model (500)."""


class UpstreamFailure(AppError):
    """A third party failed, e.g. the payment gateway rejected a refund (502)."""
