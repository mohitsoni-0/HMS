"""The start-up security check (LLD §3.13): HMAC key lengths and the committed dev RSA key by fingerprint.

No Django settings needed: ``validate_security_settings`` takes any object with the setting attributes, and
each test builds a production-like one under ``tmp_path``. ``tmp_path`` stands in for the image's BASE_DIR,
which ships without ``secrets/`` (.dockerignore), so the dev key must be recognised without that folder.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured

from hotel_commons.settings import (
    DEV_RSA_PUBLIC_KEY_SHA256,
    MIN_HMAC_KEY_BYTES,
    public_key_sha256,
    validate_security_settings,
)

COMMITTED_DEV_KEY = Path(__file__).resolve().parents[3] / "secrets" / "dev" / "api_rsa_private.pem"


def _config(base_dir: Path, **overrides) -> SimpleNamespace:
    values = {
        "BASE_DIR": base_dir,
        "ENVIRONMENT": "production",
        "SECRET_KEY": "production-secret",
        "JWT_SIGNING_KEY": b"j" * 32,
        "PII_ENCRYPTION_KEYS": {"k1": b"p" * 32},
        "PII_ACTIVE_KEY_ID": "k1",
        "PII_BLIND_INDEX_KEY": b"b" * 32,
        "IDEMPOTENCY_FINGERPRINT_KEY": b"f" * 32,
        "API_ENCRYPTION_MODE": "optional",
        "API_RSA_PRIVATE_KEY_PATH": str(base_dir / "keys" / "rsa.pem"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_key(base_dir: Path, pem: bytes) -> None:
    (base_dir / "keys").mkdir(exist_ok=True)
    (base_dir / "keys" / "rsa.pem").write_bytes(pem)


def _fresh_private_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # size is irrelevant to the fingerprint
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def dev_key_pem() -> bytes:
    if not COMMITTED_DEV_KEY.is_file():
        pytest.skip("the committed dev key is not in this checkout")
    return COMMITTED_DEV_KEY.read_bytes()


# --- HMAC key lengths --------------------------------------------------------------------------------------


def test_short_hmac_keys_are_refused(tmp_path):
    _write_key(tmp_path, _fresh_private_pem())
    config = _config(tmp_path, PII_BLIND_INDEX_KEY=b"b" * 16, IDEMPOTENCY_FINGERPRINT_KEY=b"f")

    with pytest.raises(ImproperlyConfigured) as excinfo:
        validate_security_settings(config)

    message = str(excinfo.value)
    for name in ("PII_BLIND_INDEX_KEY", "IDEMPOTENCY_FINGERPRINT_KEY"):
        assert f"{name} must be at least {MIN_HMAC_KEY_BYTES} bytes" in message
    assert "bbbb" not in message


def test_hmac_keys_of_32_bytes_are_accepted(tmp_path):
    _write_key(tmp_path, _fresh_private_pem())

    validate_security_settings(_config(tmp_path))


# --- committed dev RSA key ---------------------------------------------------------------------------------


def test_embedded_fingerprint_matches_the_committed_dev_key(dev_key_pem):
    assert public_key_sha256(dev_key_pem) in DEV_RSA_PUBLIC_KEY_SHA256


def test_dev_rsa_key_copied_into_an_image_layout_is_refused(tmp_path, dev_key_pem):
    _write_key(tmp_path, dev_key_pem)  # BASE_DIR has no secrets/dev, as in the image

    with pytest.raises(ImproperlyConfigured, match="API_RSA_PRIVATE_KEY_PATH"):
        validate_security_settings(_config(tmp_path))


def test_dev_rsa_key_is_refused_by_relative_path_too(tmp_path, dev_key_pem):
    _write_key(tmp_path, dev_key_pem)

    with pytest.raises(ImproperlyConfigured, match="API_RSA_PRIVATE_KEY_PATH"):
        validate_security_settings(_config(tmp_path, API_RSA_PRIVATE_KEY_PATH="keys/rsa.pem"))


@pytest.mark.parametrize("overrides", [{"API_ENCRYPTION_MODE": "off"}, {"ENVIRONMENT": "Local"}])
def test_dev_rsa_key_is_allowed_with_encryption_off_or_locally(tmp_path, dev_key_pem, overrides):
    _write_key(tmp_path, dev_key_pem)

    validate_security_settings(_config(tmp_path, **overrides))


@pytest.mark.parametrize("pem", [b"not a pem", None])
def test_another_key_or_an_unloadable_file_is_not_the_dev_key(tmp_path, pem):
    _write_key(tmp_path, pem if pem is not None else _fresh_private_pem())

    validate_security_settings(_config(tmp_path))
