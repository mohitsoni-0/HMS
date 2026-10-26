"""Owner account: the commercial owner of one or many properties (LLD §4.2, HLD A8).

A standalone hotelier is an account with one property and a chain an account with many: one code path.
Contact details are PII. They never appear in ``repr`` or in error messages.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Self
from uuid import UUID

from hotel_commons.domain.validation import is_indian_mobile, is_valid_email, normalize_email
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED
from uuid6 import uuid7

from catalog.constants import OWNER_DISPLAY_NAME_MAX_LENGTH, OWNER_DISPLAY_NAME_MIN_LENGTH, OWNER_USER_ID_MAX_LENGTH
from catalog.status_code import INVALID_CONTACT


@dataclass(frozen=True, slots=True)
class ContactDetails:
    """Owner contact: an e-mail address (stored lower-cased) and an Indian mobile number.

    Validated with the shared ``hotel_commons.domain.validation`` rules, like a guest's contact in booking.
    """

    email: str = field(repr=False)
    phone: str = field(repr=False)

    def __post_init__(self) -> None:
        email, phone = normalize_email(self.email), self.phone.strip()
        if not is_valid_email(email):
            raise ValidationFailed(INVALID_CONTACT, reason="contact_email is not a valid e-mail address")
        if not is_indian_mobile(phone):
            raise ValidationFailed(INVALID_CONTACT, reason="contact_phone must be a 10-digit mobile number")
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "phone", phone)


@dataclass(slots=True)
class OwnerAccount:
    id: UUID
    display_name: str
    owner_user_id: str
    contact: ContactDetails
    is_active: bool = True
    created_at: datetime | None = None  # set by the repository once the row exists

    @classmethod
    def open(cls, *, display_name: str, owner_user_id: str, contact: ContactDetails) -> Self:
        name = display_name.strip()
        if not OWNER_DISPLAY_NAME_MIN_LENGTH <= len(name) <= OWNER_DISPLAY_NAME_MAX_LENGTH:
            limits = f"{OWNER_DISPLAY_NAME_MIN_LENGTH}-{OWNER_DISPLAY_NAME_MAX_LENGTH}"
            raise ValidationFailed(VALIDATION_FAILED, errors={"display_name": f"must be {limits} characters"})
        if not owner_user_id or len(owner_user_id) > OWNER_USER_ID_MAX_LENGTH:
            raise ValidationFailed(VALIDATION_FAILED, errors={"owner_user_id": "is not a valid user id"})
        return cls(id=uuid7(), display_name=name, owner_user_id=owner_user_id, contact=contact)
