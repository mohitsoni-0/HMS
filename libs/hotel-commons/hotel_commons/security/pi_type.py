"""Categories of personal information (LLD §3.6).

The type of a PII column drives its mask, its blind-index normalisation and the associated data its
ciphertext is bound to (``"<PI type>|<column>"``). Imports only ``hotel_commons.enum``.
"""

from hotel_commons.enum import Enum


class PiType(Enum):
    NAME = 1
    EMAIL = 2
    PHONE = 3
    UPI_VPA = 4
    OTHER = 9
