"""Contact-detail rules shared by every context's domain layer (LLD §3.1).

One copy of the Indian-mobile and e-mail rules, so a guest (booking), an owner (catalog) and a wallet
holder (payment) are validated alike whoever the caller is: the API, ``seed_demo`` or a future import.
Standard library only, like the rest of the domain kernel.
"""

import re
from typing import Final

# re.ASCII keeps \d to 0-9 (a str pattern would also accept other scripts' digits). Match with
# ``is_indian_mobile`` (fullmatch): ``$`` alone would also accept a trailing newline.
INDIAN_MOBILE_PATTERN: Final = re.compile(r"^[6-9]\d{9}$", re.ASCII)
MAX_EMAIL_LENGTH: Final = 254  # RFC 5321
# local@domain.tld: no whitespace, exactly one "@", a dot in the domain and a non-empty last label.
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")


def is_indian_mobile(value: str) -> bool:
    """True for ten ASCII digits starting with 6, 7, 8 or 9, nothing around them."""
    return isinstance(value, str) and INDIAN_MOBILE_PATTERN.fullmatch(value) is not None


def normalize_email(value: str) -> str:
    """The stored form of an address: surrounding whitespace trimmed, lower-cased."""
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    """True for a plausible ``local@domain.tld`` of at most 254 characters, checked as given (normalise first)."""
    return isinstance(value, str) and len(value) <= MAX_EMAIL_LENGTH and _EMAIL.fullmatch(value) is not None
