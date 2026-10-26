"""Page-number pagination shared by list endpoints (LLD §3.9).

A list response's ``data`` is ``{"items": [...], "page": n, "page_size": n, "total": n}``.
"""

from dataclasses import dataclass
from typing import Any, Final

from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED

MAX_PAGE_SIZE: Final = 50


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A 1-based page of at most ``MAX_PAGE_SIZE`` items; out-of-range values raise ValidationFailed."""

    page: int = 1
    page_size: int = 20

    def __post_init__(self) -> None:
        errors = {}
        if self.page < 1:
            errors["page"] = "must be at least 1"
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            errors["page_size"] = f"must be between 1 and {MAX_PAGE_SIZE}"
        if errors:
            raise ValidationFailed(VALIDATION_FAILED, errors=errors)


def page_data(items: list[Any], *, page: int, page_size: int, total: int) -> dict[str, Any]:
    """The ``data`` of a paged response."""
    return {"items": items, "page": page, "page_size": page_size, "total": total}
