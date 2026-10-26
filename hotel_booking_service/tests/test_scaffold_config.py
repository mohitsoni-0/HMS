"""Contract tests for the committed local-run configuration.

The scaffold ships DEV-ONLY configuration that other code consumes at start-up: docker compose loads
``.env.example`` into the containers, the shared base settings fail fast on missing or malformed keys
(LLD §3.13), and the API payload cipher needs an unencrypted RSA key of at least 3072 bits (LLD §3.7).
These tests catch a broken value in milliseconds instead of at container start. They need neither
Django nor a database.
"""

import base64
import json
import os
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DEV_RSA_PRIVATE_KEY = REPO_ROOT / "secrets" / "dev" / "api_rsa_private.pem"

# Non-secret local values from LLD §13: they point the containers at the compose services.
LOCAL_SETTINGS = {
    "ENVIRONMENT": "local",
    "DJANGO_DEBUG": "false",
    "API_VERSION": "v1",
    "POSTGRES_NAME": "hotel",
    "POSTGRES_USER": "hotel",
    "POSTGRES_PASSWORD": "hotel",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "REDIS_URL": "redis://redis:6379/0",
    "JWT_ISSUER": "hotel-booking",
    "JWT_AUDIENCE": "hotel-booking-api",
    "JWT_TTL_SECONDS": "3600",
    "PII_ACTIVE_KEY_ID": "k1",
    "API_ENCRYPTION_MODE": "optional",
    "API_RSA_PRIVATE_KEY_PATH": "secrets/dev/api_rsa_private.pem",
    "RATE_LIMIT_ENABLED": "true",
    "TRUSTED_PROXY_COUNT": "0",
    "DEV_TOKENS_ALLOW_ADMIN": "true",
    "BOOKING_HOLD_TTL_SECONDS": "900",
    "INVENTORY_HORIZON_DAYS": "365",
    "MAX_STAY_NIGHTS": "30",
}
SECRET_SETTINGS = (
    "DJANGO_SECRET_KEY",
    "JWT_SIGNING_KEY",
    "PII_ENCRYPTION_KEYS",
    "PII_BLIND_INDEX_KEY",
    "IDEMPOTENCY_FINGERPRINT_KEY",
)
PII_KEY_ID = re.compile(r"[A-Za-z0-9]{1,16}")


def read_env_file(path: Path) -> list[tuple[str, str]]:
    """Return the KEY=VALUE entries of an env file in order, skipping blank lines and comments."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        assert separator, f"not a KEY=VALUE line: {line!r}"
        entries.append((name.strip(), value.strip()))
    return entries


def decode_key(value: str) -> bytes:
    """Decode base64 key material, insisting it reads the same as standard and URL-safe base64."""
    raw = base64.b64decode(value, validate=True)
    assert base64.urlsafe_b64decode(value) == raw, "key must decode the same under either alphabet"
    return raw


@pytest.fixture(scope="module")
def env_example() -> dict[str, str]:
    return dict(read_env_file(ENV_EXAMPLE))


def test_env_example_declares_every_variable_once() -> None:
    names = [name for name, _ in read_env_file(ENV_EXAMPLE)]

    assert len(names) == len(set(names)), "a variable is declared twice"
    assert set(LOCAL_SETTINGS) | set(SECRET_SETTINGS) <= set(names)


def test_env_example_targets_the_compose_services(env_example: dict[str, str]) -> None:
    assert {name: env_example[name] for name in LOCAL_SETTINGS} == LOCAL_SETTINGS


def test_env_example_values_are_unquoted(env_example: dict[str, str]) -> None:
    # docker compose strips surrounding quotes but a plain KEY=VALUE loader keeps them,
    # so only unquoted values read identically everywhere.
    assert [name for name, value in env_example.items() if value[:1] in {"'", '"'}] == []


def test_env_example_keys_are_distinct_32_byte_keys(env_example: dict[str, str]) -> None:
    key_ring = json.loads(env_example["PII_ENCRYPTION_KEYS"])
    keys = [
        env_example["JWT_SIGNING_KEY"],
        *key_ring.values(),
        env_example["PII_BLIND_INDEX_KEY"],
        env_example["IDEMPOTENCY_FINGERPRINT_KEY"],
    ]

    decoded = [decode_key(key) for key in keys]

    assert all(PII_KEY_ID.fullmatch(key_id) for key_id in key_ring)
    assert env_example["PII_ACTIVE_KEY_ID"] in key_ring
    assert [len(key) for key in decoded] == [32] * len(keys)
    assert len(set(decoded)) == len(decoded), "every purpose needs its own key"


def test_env_example_django_secret_key_passes_the_deploy_check(env_example: dict[str, str]) -> None:
    # Same thresholds as Django's security.W009 check.
    secret_key = env_example["DJANGO_SECRET_KEY"]

    assert len(secret_key) >= 50
    assert len(set(secret_key)) >= 5
    assert not secret_key.startswith("django-insecure-")


def test_dev_rsa_key_is_unencrypted_pkcs8_rsa_3072(env_example: dict[str, str]) -> None:
    pem = DEV_RSA_PRIVATE_KEY.read_bytes()

    key = serialization.load_pem_private_key(pem, password=None)

    assert REPO_ROOT / env_example["API_RSA_PRIVATE_KEY_PATH"] == DEV_RSA_PRIVATE_KEY
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 3072


def test_docker_build_context_excludes_env_files_and_keys() -> None:
    # Keys reach containers at runtime (env_file, read-only mount), never through an image layer.
    ignored = {line.strip() for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}

    assert {".env", "secrets/"} <= ignored


@pytest.mark.parametrize(
    "script",
    ["manage.py", "docker/entrypoint.sh", "docker/scheduler.sh", "local_run.sh", "run-tests.sh"],
)
def test_entry_scripts_are_executable(script: str) -> None:
    assert os.access(REPO_ROOT / script, os.X_OK)
