"""Payment enums shared with other contexts (LLD §9.1).

SMALLINT in the database through the model ``EnumField``, member names on the wire through ``HotelEnumField``.
Booking imports these from here; ``payment.enums`` re-exports them for the app itself.
"""

from hotel_commons.enum import Enum


class PaymentMethod(Enum):
    CARD = 1
    UPI = 2
    WALLET = 3


class PaymentStatus(Enum):
    INITIATED = 1
    SUCCEEDED = 2
    FAILED = 3
    PARTIALLY_REFUNDED = 4
    REFUNDED = 5


class RefundStatus(Enum):
    SUCCEEDED = 1


class WalletProvider(Enum):
    PAYTM = 1
    PHONEPE = 2
    AMAZON_PAY = 3


class CardBrand(Enum):
    VISA = 1
    MASTERCARD = 2
    RUPAY = 3
    AMEX = 4
