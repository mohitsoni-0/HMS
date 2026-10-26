"""``LOGGING`` dictConfig factory (LLD §3.13).

One console handler; every record passes ``RequestIdFilter`` then ``PiiRedactionFilter``, so each line
carries ``request_id`` and ``actor`` and has PII masked. The ``django`` logger is reconfigured to send its
records through that handler (instead of Django's default handlers), and SQL logging stays at WARNING
because query parameters can hold PII.
"""

from typing import Any, Final

LOG_LEVELS: Final = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s actor=%(actor)s %(message)s"


def build_logging_config(level: str = "INFO") -> dict[str, Any]:
    """The ``LOGGING`` setting for ``level`` (case-insensitive); an unknown level raises ValueError."""
    normalised = level.upper()
    if normalised not in LOG_LEVELS:
        raise ValueError(f"Unknown log level {level!r}; use one of {', '.join(LOG_LEVELS)}")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_context": {"()": "hotel_commons.logging.filters.RequestIdFilter"},
            "pii_redaction": {"()": "hotel_commons.logging.filters.PiiRedactionFilter"},
        },
        "formatters": {"standard": {"format": LOG_FORMAT}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "filters": ["request_context", "pii_redaction"],
            },
        },
        "root": {"handlers": ["console"], "level": normalised},
        "loggers": {
            "django": {"handlers": [], "level": normalised, "propagate": True},
            "django.server": {"handlers": [], "level": normalised, "propagate": True},
            "django.db.backends": {"level": "WARNING"},
        },
    }
