"""Booking enums (LLD §8.1).

SMALLINT in the database through the model ``EnumField``, member names on the wire through ``HotelEnumField``.
No other context imports these: booking publishes events, not an API.
"""

from hotel_commons.enum import Enum


class BookingStatus(Enum):
    PENDING_PAYMENT = 1
    CONFIRMED = 2
    CANCELLED = 3
    EXPIRED = 4


class BookingType(Enum):
    PREPAID = 1
    PAY_AT_PROPERTY = 2


class BookingTrigger(Enum):
    CONFIRM_PAYMENT = 1
    CANCEL = 2
    EXPIRE = 3


class BookingRefundStatus(Enum):
    NONE = 1
    REFUNDED = 2
