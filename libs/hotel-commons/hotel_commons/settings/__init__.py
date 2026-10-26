"""Settings toolkit: typed environment readers, key decoding and the start-up security check (LLD §3.13).

``base_settings`` reads the environment through these helpers, so a malformed value (bad base64, a mode
that does not exist, a negative count) fails with ImproperlyConfigured the moment settings load.
``validate_security_settings`` runs from ``HotelCommonsConfig.ready()``: by then every settings module,
test settings included, has finished, so it judges the values the process will really use.
"""

import base64
import binascii
import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final, TypeVar

from django.core.exceptions import ImproperlyConfigured

from hotel_commons.consts import API_ENCRYPTION_OFF, ENVIRONMENT_LOCAL, ENVIRONMENT_PRODUCTION

T = TypeVar("T")

MIN_JWT_KEY_BYTES: Final = 32
PII_KEY_BYTES: Final = 32
DEV_ENV_FILE: Final = ".env.example"
DEV_SECRETS_DIR: Final = Path("secrets", "dev")

_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})
_HMAC_KEY_SETTINGS: Final = ("PII_BLIND_INDEX_KEY", "IDEMPOTENCY_FINGERPRINT_KEY")
_SINGLE_KEY_SETTINGS: Final = ("JWT_SIGNING_KEY", *_HMAC_KEY_SETTINGS)


# --- Project location and .env ------------------------------------------------------------------------------


def find_base_dir() -> Path:
    """``DJANGO_BASE_DIR`` when set, else the nearest directory from the CWD upwards holding manage.py, else the CWD."""
    configured = os.environ.get("DJANGO_BASE_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    cwd = Path.cwd().resolve()
    return next((folder for folder in (cwd, *cwd.parents) if (folder / "manage.py").is_file()), cwd)


def resolve_path(value: str, base_dir: Path) -> Path:
    """``value`` as an absolute path, relative paths being taken from ``base_dir``."""
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def parse_dotenv(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` entries of an env file. Blank lines, ``#`` comments and lines without ``=`` are skipped;
    an ``export`` prefix and one pair of surrounding quotes are removed. Values are taken verbatim otherwise."""
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.removeprefix("export ").strip()
        if name:
            entries[name] = _unquote(value.strip())
    return entries


def load_dotenv(path: Path) -> None:
    """``os.environ.setdefault`` for every entry of ``path`` when it exists: real environment variables win."""
    if path.is_file():
        for name, value in parse_dotenv(path).items():
            os.environ.setdefault(name, value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# --- Typed environment readers (an empty value counts as unset) ---------------------------------------------


def read_str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def read_bool(name: str, *, default: bool) -> bool:
    raw = read_str(name).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ImproperlyConfigured(f"{name} must be one of true/false/1/0/yes/no/on/off")


def read_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = read_str(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer") from None
    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f"{name} must be >= {minimum}")
    return value


def read_csv(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in read_str(name, default).split(",") if item.strip()]


def read_choice(name: str, default: str, choices: Iterable[str]) -> str:
    """One of ``choices``, matched case-insensitively and returned in its canonical spelling."""
    canonical = {choice.lower(): choice for choice in choices}
    raw = read_str(name, default)
    if raw.lower() not in canonical:
        raise ImproperlyConfigured(f"{name} must be one of {', '.join(canonical.values())}")
    return canonical[raw.lower()]


def read_key(name: str) -> bytes | None:
    """A base64 key (standard or URL-safe alphabet, padding optional) as bytes; None when unset."""
    raw = read_str(name)
    if not raw:
        return None
    try:
        return decode_base64_key(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be base64-encoded key material") from None


def read_key_ring(name: str) -> dict[str, bytes]:
    """A JSON object of key id -> base64 key, e.g. ``{"k1": "<base64>"}``; {} when unset."""
    raw = read_str(name)
    if not raw:
        return {}
    try:
        return decode_key_ring(raw)
    except ValueError:
        raise ImproperlyConfigured(
            f'{name} must be a JSON object of key id -> base64 key, e.g. {{"k1": "..."}}'
        ) from None


def decode_base64_key(value: str) -> bytes:
    """Decode standard or URL-safe base64, with or without padding; ValueError when it is not base64."""
    text = "".join(value.split()).replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except binascii.Error as exc:
        raise ValueError("not base64") from exc


def decode_key_ring(value: str) -> dict[str, bytes]:
    """Decode ``{"<key id>": "<base64 key>", ...}``; ValueError when it has another shape."""
    ring = json.loads(value)
    if not isinstance(ring, dict) or not all(isinstance(key, str) for key in ring.values()):
        raise ValueError("expected a JSON object of base64 strings")
    return {key_id: decode_base64_key(key) for key_id, key in ring.items()}


# --- Start-up security check --------------------------------------------------------------------------------


def validate_security_settings(config: Any) -> None:
    """Raise ImproperlyConfigured listing every problem, or return when the keys are fit to use (LLD §3.13).

    ``config`` is ``django.conf.settings`` (any object with the setting attributes). Refused: a missing or
    sub-32-byte JWT_SIGNING_KEY; a PII key that is not 32 bytes; a PII_ACTIVE_KEY_ID outside the key ring;
    a missing PII_BLIND_INDEX_KEY or IDEMPOTENCY_FINGERPRINT_KEY; and, unless ENVIRONMENT is "local", any
    committed dev key: a value from BASE_DIR/.env.example, or the RSA key under BASE_DIR/secrets/dev (even
    copied elsewhere) while API payload encryption is on. Messages name settings, never key material.
    """
    problems = [*_key_problems(config), *_dev_key_problems(config)]
    if problems:
        raise ImproperlyConfigured("Refusing to start: " + "; ".join(problems))


def _key_problems(config: Any) -> list[str]:
    problems = []
    jwt_key = getattr(config, "JWT_SIGNING_KEY", None)
    if not isinstance(jwt_key, bytes) or len(jwt_key) < MIN_JWT_KEY_BYTES:
        problems.append(f"JWT_SIGNING_KEY must be at least {MIN_JWT_KEY_BYTES} bytes")
    ring = _key_ring(config)
    problems += [
        f"PII_ENCRYPTION_KEYS[{key_id!r}] must be exactly {PII_KEY_BYTES} bytes"
        for key_id, key in ring.items()
        if not isinstance(key, bytes) or len(key) != PII_KEY_BYTES
    ]
    if not ring:
        problems.append("PII_ENCRYPTION_KEYS is missing")
    elif getattr(config, "PII_ACTIVE_KEY_ID", None) not in ring:
        problems.append("PII_ACTIVE_KEY_ID must name a key in PII_ENCRYPTION_KEYS")
    for name in _HMAC_KEY_SETTINGS:
        value = getattr(config, name, None)
        if not isinstance(value, bytes) or not value:
            problems.append(f"{name} is missing")
    return problems


def _dev_key_problems(config: Any) -> list[str]:
    if str(getattr(config, "ENVIRONMENT", ENVIRONMENT_PRODUCTION)).lower() == ENVIRONMENT_LOCAL:
        return []
    base_dir = Path(getattr(config, "BASE_DIR", Path.cwd()))
    committed = _committed_dev_secrets(base_dir / DEV_ENV_FILE)
    problems = [
        f"{name} uses a committed dev key from {DEV_ENV_FILE}"
        for name, values in _effective_secrets(config).items()
        if committed.intersection(values)
    ]
    if _uses_dev_rsa_key(config, base_dir):
        problems.append(f"API_RSA_PRIVATE_KEY_PATH holds the committed dev key from {DEV_SECRETS_DIR}")
    return problems


def _key_ring(config: Any) -> Mapping[str, object]:
    ring = getattr(config, "PII_ENCRYPTION_KEYS", None)
    return ring if isinstance(ring, Mapping) else {}


def _effective_secrets(config: Any) -> dict[str, list[bytes]]:
    secrets: dict[str, list[bytes]] = {}
    for name in _SINGLE_KEY_SETTINGS:
        value = getattr(config, name, None)
        if isinstance(value, bytes):
            secrets[name] = [value]
    secrets["PII_ENCRYPTION_KEYS"] = [key for key in _key_ring(config).values() if isinstance(key, bytes)]
    secret_key = _secret_key(config)
    if secret_key:
        secrets["SECRET_KEY"] = [secret_key.encode()]
    return secrets


def _secret_key(config: Any) -> str:
    try:
        secret_key = getattr(config, "SECRET_KEY", "")
    except ImproperlyConfigured:  # django.conf.settings raises instead of returning an empty SECRET_KEY
        return ""
    return secret_key if isinstance(secret_key, str) else ""


def _committed_dev_secrets(env_file: Path) -> frozenset[bytes]:
    """Every key committed in the example env file, decoded the way base_settings decodes it."""
    if not env_file.is_file():
        return frozenset()
    entries = parse_dotenv(env_file)
    found: set[bytes] = set()
    for name in _SINGLE_KEY_SETTINGS:
        found.update(_decoded_or_nothing(decode_base64_key, entries.get(name)))
    for ring in _decoded_or_nothing(decode_key_ring, entries.get("PII_ENCRYPTION_KEYS")):
        found.update(ring.values())
    if entries.get("DJANGO_SECRET_KEY"):
        found.add(entries["DJANGO_SECRET_KEY"].encode())
    found.discard(b"")
    return frozenset(found)


def _decoded_or_nothing(decoder: Callable[[str], T], value: str | None) -> list[T]:
    if not value:
        return []
    try:
        return [decoder(value)]
    except ValueError:  # an undecodable example value cannot be in use, since settings refuse it at load
        return []


def _uses_dev_rsa_key(config: Any, base_dir: Path) -> bool:
    if getattr(config, "API_ENCRYPTION_MODE", API_ENCRYPTION_OFF) == API_ENCRYPTION_OFF:
        return False
    configured = getattr(config, "API_RSA_PRIVATE_KEY_PATH", "")
    key_file = resolve_path(str(configured), base_dir) if configured else None
    dev_dir = base_dir / DEV_SECRETS_DIR
    if key_file is None or not key_file.is_file() or not dev_dir.is_dir():
        return False
    key = key_file.read_bytes()
    return any(key == dev_key.read_bytes() for dev_key in dev_dir.glob("*.pem"))
