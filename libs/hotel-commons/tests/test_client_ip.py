"""Client IP behind trusted proxies (LLD §3.7, §3.10): only hops added by our own proxies are believed.

No database. Without the project settings (``-p no:django``) a minimal Django is configured here; the one
setting under test is overridden per test either way.
"""

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=["django.contrib.contenttypes", "rest_framework"],
        USE_TZ=True,
        TIME_ZONE="Asia/Kolkata",
        ALLOWED_HOSTS=["testserver"],
        REST_FRAMEWORK={"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"},
    )
django.setup()

import pytest  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402

from hotel_commons.security.client_ip import client_ip  # noqa: E402


def _request(remote_addr: str = "10.0.0.9", forwarded_for: str | None = None):
    extra = {"REMOTE_ADDR": remote_addr}
    if forwarded_for is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded_for
    return RequestFactory().get("/hotel/v1/health", **extra)


@override_settings(TRUSTED_PROXY_COUNT=0)
def test_no_trusted_proxy_uses_remote_addr_and_ignores_forwarded_for():
    assert client_ip(_request(forwarded_for="1.2.3.4")) == "10.0.0.9"


@override_settings(TRUSTED_PROXY_COUNT=1)
def test_client_ip_ignores_spoofed_leftmost_hop():
    # The client sent "X-Forwarded-For: 6.6.6.6"; our one proxy appended the address it really saw.
    assert client_ip(_request(forwarded_for="6.6.6.6, 203.0.113.7")) == "203.0.113.7"


@override_settings(TRUSTED_PROXY_COUNT=2)
def test_two_trusted_proxies_take_the_second_hop_from_the_right():
    assert client_ip(_request(forwarded_for="6.6.6.6, 203.0.113.7, 10.0.0.2")) == "203.0.113.7"


@override_settings(TRUSTED_PROXY_COUNT=2)
def test_too_few_hops_fall_back_to_remote_addr():
    assert client_ip(_request(forwarded_for="203.0.113.7")) == "10.0.0.9"


@override_settings(TRUSTED_PROXY_COUNT=1)
@pytest.mark.parametrize("hop", ["not-an-ip", "", "203.0.113.7:443"])
def test_invalid_hop_falls_back_to_remote_addr(hop):
    assert client_ip(_request(forwarded_for=f"6.6.6.6, {hop}")) == "10.0.0.9"


@override_settings(TRUSTED_PROXY_COUNT=1)
def test_ipv6_hop_is_normalised():
    assert client_ip(_request(forwarded_for="2001:DB8::0:1")) == "2001:db8::1"


@override_settings(TRUSTED_PROXY_COUNT=0)
def test_unusable_remote_addr_is_unknown():
    assert client_ip(_request(remote_addr="")) == "unknown"
