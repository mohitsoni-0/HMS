"""The shared contact rules (hotel_commons.domain.validation): one Indian-mobile and one e-mail rule for
every context. Pure Python."""

import pytest

from hotel_commons.domain.validation import (
    INDIAN_MOBILE_PATTERN,
    is_indian_mobile,
    is_valid_email,
    normalize_email,
)


@pytest.mark.parametrize("value", ["9876543210", "6000000000", "7123456789", "8999999999"])
def test_indian_mobile_accepts_ten_digits_starting_6_to_9(value):
    assert is_indian_mobile(value)
    assert INDIAN_MOBILE_PATTERN.match(value)


@pytest.mark.parametrize(
    "value",
    ["5876543210", "987654321", "98765432101", "+919876543210", " 9876543210", "9876543210\n",
     "98765-43210", "९८७६५४३२१०", "9८७६५४३२१०", "", None, 9876543210],
)
def test_indian_mobile_rejects_anything_else(value):
    assert not is_indian_mobile(value)


def test_normalize_email_trims_and_lower_cases():
    assert normalize_email("  Guest@Example.COM \n") == "guest@example.com"


@pytest.mark.parametrize("value", ["a@b.co", "first.last+tag@mail.example.in", "x_y@sub.domain.org"])
def test_valid_email(value):
    assert is_valid_email(value)


@pytest.mark.parametrize(
    "value",
    # "a@mail.co." is the case booking and catalog used to disagree on (review D10).
    ["a@mail.co.", "plain", "a@b", "a@@b.co", "a b@c.co", "a@b .co", " a@b.co", "@b.co", "a@.", "", None,
     "a@" + "b" * 250 + ".co"],
)
def test_invalid_email(value):
    assert not is_valid_email(value)


def test_email_length_limit_is_254():
    local = "a" * (254 - len("@b.co"))
    assert is_valid_email(f"{local}@b.co")
    assert not is_valid_email(f"a{local}@b.co")
