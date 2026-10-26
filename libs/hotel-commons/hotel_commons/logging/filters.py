"""Logging filters (LLD §3.13): request correlation and a PII safety net.

``RequestIdFilter`` stamps every record with the request id and the actor. ``PiiRedactionFilter`` masks
emails and UPI ids, Indian mobile numbers, 13-19 digit card numbers and cvv values in the rendered message,
the actor and any traceback. Services log ids only (LLD §2.8); this filter catches what slips through.
Neither filter drops a record, and neither may raise into the code that is logging.
"""

import logging
import re
from collections.abc import Mapping
from typing import Final

from hotel_commons.context import RequestContext

NO_REQUEST_ID: Final = "-"

_CVV = re.compile(r"""(?i)(?P<key>["']?(?<![A-Za-z0-9])cvv["']?\s*[:=]\s*["']?)[^"'\s,}&]*""")
_EMAIL = re.compile(r"(?P<first>[A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)")
# Capturing, so that split() keeps each UUID in the result (at the odd indexes).
_UUID = re.compile(r"((?<![0-9A-Za-z])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Za-z]))")
_TRACEBACK_FORMATTER: Final = logging.Formatter()


def redact_pii(text: str) -> str:
    """Mask PII and card data in free text. A mask never matches again, so redacting twice is harmless.

    Emails and UPI ids keep their first character and domain ("r****@gmail.com"); card and phone numbers
    keep their last four digits; cvv values become "***". UUIDs, the ids services log (LLD §2.8), are kept
    whole: a UUIDv7 starts with a millisecond timestamp, whose digits would otherwise pass for a card number.
    """
    parts = _UUID.split(text)
    return "".join(part if index % 2 else _redact(part) for index, part in enumerate(parts))


def _redact(text: str) -> str:
    text = _CVV.sub(r"\g<key>***", text)
    text = _EMAIL.sub(r"\g<first>****@\g<domain>", text)
    text = _CARD.sub(_mask_card, text)
    return _PHONE.sub(_mask_phone, text)


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "*" * (len(digits) - 4) + digits[-4:]


def _mask_phone(match: re.Match[str]) -> str:
    return "******" + match.group()[-4:]


def _safe_text(value: object) -> str:
    try:
        return redact_pii(str(value))
    except Exception:  # an object whose __str__ raises; logging must not fail because of it
        return "<unprintable>"


def _redacted_args(args: tuple[object, ...] | Mapping[str, object] | None) -> tuple[object, ...] | dict[str, str]:
    if isinstance(args, Mapping):
        return {key: _safe_text(value) for key, value in args.items()}
    return tuple(_safe_text(arg) for arg in args or ())


class RequestIdFilter(logging.Filter):
    """Adds ``request_id`` ("-" outside a request) and ``actor`` to every record, unless ``extra`` set them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = RequestContext.request_id() or NO_REQUEST_ID
        if not hasattr(record, "actor"):
            record.actor = RequestContext.actor()
        return True


class PiiRedactionFilter(logging.Filter):
    """Renders the message once (``msg % args``), redacts it and keeps it with empty args.

    Rendering first covers arguments of every type (dicts, dataclasses, exceptions), not only strings. When
    the arguments do not fit the format, the record keeps its shape with every part redacted, so even the
    handler's own error report cannot leak a value.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact_pii(record.getMessage()), ()
        except Exception:  # bad format or arguments: never raise into the code that is logging
            record.msg, record.args = _safe_text(record.msg), _redacted_args(record.args)
        actor = getattr(record, "actor", None)
        if isinstance(actor, str):
            record.actor = redact_pii(actor)
        if record.exc_info and not record.exc_text:
            record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_pii(record.exc_text)
        return True
