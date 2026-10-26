"""The caller's IP address for per-IP rate limits (LLD §3.7, §3.10).

``X-Forwarded-For`` is client-controlled except for the hops our own proxies append on the right. With
``TRUSTED_PROXY_COUNT = n`` the client is the n-th hop from the right; anything further left (where a
client would put a spoofed address) is ignored. With no trusted proxy the header is ignored entirely.
"""

import ipaddress
import logging
from typing import Any, Final

from django.conf import settings

logger = logging.getLogger(__name__)

UNKNOWN_IP: Final = "unknown"


def client_ip(request: Any) -> str:
    """The validated, normalised client address; ``REMOTE_ADDR`` when the trusted hop is missing or invalid,
    ``"unknown"`` when even that is unusable."""
    remote_addr = request.META.get("REMOTE_ADDR", "")
    trusted_hop = _trusted_hop(request.META.get("HTTP_X_FORWARDED_FOR", ""), settings.TRUSTED_PROXY_COUNT)
    return _valid_ip(trusted_hop) or _valid_ip(remote_addr) or UNKNOWN_IP


def _trusted_hop(forwarded_for: str, trusted_proxy_count: int) -> str | None:
    if trusted_proxy_count == 0 or not forwarded_for:
        return None
    hops = [hop.strip() for hop in forwarded_for.split(",")]
    return hops[-trusted_proxy_count] if len(hops) >= trusted_proxy_count else None


def _valid_ip(candidate: str | None) -> str | None:
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        logger.warning("Ignoring an invalid client address")
        return None
