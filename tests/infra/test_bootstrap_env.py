"""scripts/bootstrap_env.py: a fresh local .env and RSA key that the start-up security check accepts."""

import importlib.util
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hotel_commons.settings import (
    DEV_RSA_PUBLIC_KEY_SHA256,
    decode_base64_key,
    decode_key_ring,
    parse_dotenv,
    public_key_sha256,
    validate_security_settings,
)

ROOT = Path(__file__).resolve().parents[2]
KEY_SETTINGS = (
    "DJANGO_SECRET_KEY",
    "JWT_SIGNING_KEY",
    "PII_ENCRYPTION_KEYS",
    "PII_BLIND_INDEX_KEY",
    "IDEMPOTENCY_FINGERPRINT_KEY",
)


@pytest.fixture(scope="module")
def bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bootstrap_env", ROOT / "scripts" / "bootstrap_env.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(bootstrap: ModuleType, tmp_path: Path, *extra: str) -> int:
    return bootstrap.main(["--env-file", str(tmp_path / ".env"), "--secrets-dir", str(tmp_path / "keys"), *extra])


def _as_settings(entries: dict[str, str], key_path: Path) -> SimpleNamespace:
    """The values decoded the way base_settings decodes them, judged as a production deployment would be."""
    return SimpleNamespace(
        ENVIRONMENT="production",
        BASE_DIR=ROOT,  # so the check compares against the committed .env.example keys
        SECRET_KEY=entries["DJANGO_SECRET_KEY"],
        JWT_SIGNING_KEY=decode_base64_key(entries["JWT_SIGNING_KEY"]),
        PII_ENCRYPTION_KEYS=decode_key_ring(entries["PII_ENCRYPTION_KEYS"]),
        PII_ACTIVE_KEY_ID=entries["PII_ACTIVE_KEY_ID"],
        PII_BLIND_INDEX_KEY=decode_base64_key(entries["PII_BLIND_INDEX_KEY"]),
        IDEMPOTENCY_FINGERPRINT_KEY=decode_base64_key(entries["IDEMPOTENCY_FINGERPRINT_KEY"]),
        API_ENCRYPTION_MODE="enforced",
        API_RSA_PRIVATE_KEY_PATH=str(key_path),
    )


def test_writes_fresh_keys_that_pass_the_security_check(bootstrap: ModuleType, tmp_path: Path) -> None:
    assert _run(bootstrap, tmp_path) == 0

    entries = parse_dotenv(tmp_path / ".env")
    key_file = tmp_path / "keys" / "api_rsa_private.pem"
    assert entries["API_RSA_PRIVATE_KEY_PATH"] == str(key_file)  # outside the project: an absolute path
    validate_security_settings(_as_settings(entries, key_file))  # not short, not the committed dev keys
    private_key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey) and private_key.key_size == 3072
    assert public_key_sha256(key_file.read_bytes()) not in DEV_RSA_PUBLIC_KEY_SHA256
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert "APP_PORT" not in entries


def test_every_run_generates_different_keys(bootstrap: ModuleType, tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert _run(bootstrap, first) == 0 and _run(bootstrap, second) == 0

    first_entries, second_entries = parse_dotenv(first / ".env"), parse_dotenv(second / ".env")
    for name in KEY_SETTINGS:
        assert first_entries[name] != second_entries[name], name


def test_refuses_to_overwrite_without_force(
    bootstrap: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=8001\n")

    assert _run(bootstrap, tmp_path) == 1

    assert env_file.read_text() == "APP_PORT=8001\n"
    assert not (tmp_path / "keys" / "api_rsa_private.pem").exists()
    assert "--force" in capsys.readouterr().err


def test_refuses_when_only_the_key_exists(bootstrap: ModuleType, tmp_path: Path) -> None:
    key_file = tmp_path / "keys" / "api_rsa_private.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(b"existing")

    assert _run(bootstrap, tmp_path) == 1
    assert key_file.read_bytes() == b"existing" and not (tmp_path / ".env").exists()


def test_force_replaces_and_keeps_the_app_port(bootstrap: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_PORT=8001\nJWT_SIGNING_KEY=old\n")

    assert _run(bootstrap, tmp_path, "--force") == 0

    entries = parse_dotenv(tmp_path / ".env")
    assert entries["APP_PORT"] == "8001" and entries["JWT_SIGNING_KEY"] != "old"
    assert _run(bootstrap, tmp_path, "--force", "--app-port", "9000") == 0
    assert parse_dotenv(tmp_path / ".env")["APP_PORT"] == "9000"


def test_key_path_inside_the_project_is_relative(bootstrap: ModuleType) -> None:
    assert bootstrap._key_path_setting(ROOT / "secrets" / "local" / "api_rsa_private.pem") == (
        "secrets/local/api_rsa_private.pem"  # what the container resolves under /app (compose mounts secrets/)
    )
