"""Discovery status codes (LLD §7.3): ``21 04 EEE``."""

from hotel_commons.status_code import StatusCode

INVALID_SEARCH = StatusCode(2104001, "{reason}", 400)
