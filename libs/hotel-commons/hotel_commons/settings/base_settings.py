"""Base Django settings for every service built on hotel_commons (LLD §3.13).

The project's settings module does ``from hotel_commons.settings.base_settings import *`` and then adds its
apps, ROOT_URLCONF and WSGI_APPLICATION. Every value comes from the environment; a ``.env`` file in BASE_DIR
fills the gaps through ``os.environ.setdefault``, so real environment variables always win.

A malformed value fails right here with ImproperlyConfigured. Missing, short and committed dev keys are
refused at start-up by ``validate_security_settings`` (run from ``HotelCommonsConfig.ready()``), after every
override, test settings included, has applied. Helpers are imported under private names, so ``import *``
copies settings and nothing else.
"""

import hotel_commons.consts as _consts
import hotel_commons.settings as _env
from hotel_commons.domain.clock import BUSINESS_TIME_ZONE as _BUSINESS_TIME_ZONE
from hotel_commons.logging.config import LOG_LEVELS as _LOG_LEVELS
from hotel_commons.logging.config import build_logging_config as _build_logging_config

# --- Project and environment -------------------------------------------------------------------------------

BASE_DIR = _env.find_base_dir()
_env.load_dotenv(BASE_DIR / ".env")

ENVIRONMENT = _env.read_str("ENVIRONMENT", _consts.ENVIRONMENT_PRODUCTION).lower()
_IS_LOCAL = ENVIRONMENT == _consts.ENVIRONMENT_LOCAL

SECRET_KEY = _env.read_str("DJANGO_SECRET_KEY")
DEBUG = _env.read_bool("DJANGO_DEBUG", default=False)
# Any host locally; elsewhere the hosts must be listed (an empty list makes Django answer 400 to everything).
ALLOWED_HOSTS = _env.read_csv("DJANGO_ALLOWED_HOSTS", "*" if _IS_LOCAL else "")
API_VERSION = _env.read_str("API_VERSION", "v1")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "hotel_commons",
]

# Order matters (LLD §3.8): request context first, then the last-resort error envelope, then payload
# decryption, and authentication last, so it sees the decrypted request.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "hotel_commons.middlewares.request_context.RequestContextMiddleware",
    "hotel_commons.middlewares.exception_handling.ExceptionHandlingMiddleware",
    "hotel_commons.middlewares.api_encryption.ApiEncryptionMiddleware",
    "hotel_commons.middlewares.authentication.JWTAuthenticationMiddleware",
]

# Only the Swagger UI page renders a template.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

# --- Data stores -------------------------------------------------------------------------------------------

# A contended booking fails fast (409 RESOURCE_BUSY / 503 SERVICE_TIMEOUT) instead of queueing behind a lock.
# 0 leaves a limit unset: docker/entrypoint.sh runs ``migrate`` that way, so a long CREATE INDEX or a lock
# wait behind live traffic does not abort the deploy.
DB_LOCK_TIMEOUT_MS = _env.read_int("DB_LOCK_TIMEOUT_MS", 2000, minimum=0)
DB_STATEMENT_TIMEOUT_MS = _env.read_int("DB_STATEMENT_TIMEOUT_MS", 5000, minimum=0)
_DB_SESSION_OPTIONS = " ".join(
    f"-c {name}={milliseconds}"
    for name, milliseconds in (("lock_timeout", DB_LOCK_TIMEOUT_MS), ("statement_timeout", DB_STATEMENT_TIMEOUT_MS))
    if milliseconds
)

# One database for every bounded context, one schema each, all through this single alias, so cross-context
# use cases stay one ACID transaction (LLD §3.15). The backend is Django's PostgreSQL one with introspection
# that resolves the schema-qualified ``db_table`` names, so migrations can alter their indexes and keys.
DATABASES = {
    "default": {
        "ENGINE": "hotel_commons.db.backends.postgresql",
        "NAME": _env.read_str("POSTGRES_NAME", "hotel"),
        "USER": _env.read_str("POSTGRES_USER", "hotel"),
        "PASSWORD": _env.read_str("POSTGRES_PASSWORD"),
        "HOST": _env.read_str("POSTGRES_HOST", "localhost"),
        "PORT": _env.read_int("POSTGRES_PORT", 5432, minimum=1),
        # Services commit outcomes (a declined payment, an expired hold) that a request must not roll back.
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"options": _DB_SESSION_OPTIONS} if _DB_SESSION_OPTIONS else {},
    },
}

REDIS_URL = _env.read_str("REDIS_URL", "redis://localhost:6379/0")

# --- API ---------------------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["hotel_commons.api.authentication.PrincipalAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["hotel_commons.security.permissions.IsAuthenticatedPrincipal"],
    "DEFAULT_THROTTLE_CLASSES": ["hotel_commons.api.throttling.RedisRateLimitThrottle"],
    "EXCEPTION_HANDLER": "hotel_commons.api.exception_handler.envelope_exception_handler",
    # OPTIONS answers 405 rather than DRF metadata (which fails on viewsets without a serializer_class).
    "DEFAULT_METADATA_CLASS": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "UNAUTHENTICATED_USER": None,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",  # the API parses JSON only, so test clients must send JSON
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Hotel Booking Service API",
    "DESCRIPTION": (
        "Discovery, property onboarding, booking, payment and cancellation. Every response is an envelope "
        "{status, message, data, request_id}; amounts are rupee strings with a sibling currency."
    ),
    "VERSION": API_VERSION,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SERVE_AUTHENTICATION": [],
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# --- Identity and keys (bytes after base64 decoding; validated at start-up) --------------------------------

JWT_SIGNING_KEY = _env.read_key("JWT_SIGNING_KEY")
JWT_ISSUER = _env.read_str("JWT_ISSUER", "hotel-booking")
JWT_AUDIENCE = _env.read_str("JWT_AUDIENCE", "hotel-booking-api")
JWT_TTL_SECONDS = _env.read_int("JWT_TTL_SECONDS", 3600, minimum=1)
JWT_LEEWAY_SECONDS = _env.read_int("JWT_LEEWAY_SECONDS", 30, minimum=0)

PII_ENCRYPTION_KEYS = _env.read_key_ring("PII_ENCRYPTION_KEYS")
PII_ACTIVE_KEY_ID = _env.read_str("PII_ACTIVE_KEY_ID")
PII_BLIND_INDEX_KEY = _env.read_key("PII_BLIND_INDEX_KEY")
IDEMPOTENCY_FINGERPRINT_KEY = _env.read_key("IDEMPOTENCY_FINGERPRINT_KEY")

# --- API payload encryption --------------------------------------------------------------------------------

API_ENCRYPTION_MODE = _env.read_choice(
    "API_ENCRYPTION_MODE", _consts.API_ENCRYPTION_OPTIONAL, _consts.API_ENCRYPTION_MODES
)
_API_ROOT = f"/hotel/{API_VERSION}"
API_ENCRYPTION_BYPASS_PATHS = frozenset(
    f"{_API_ROOT}/{path}" for path in ("health", "security/public-key", "schema", "docs", "dev/tokens")
)
API_RSA_PRIVATE_KEY_PATH = str(
    _env.resolve_path(_env.read_str("API_RSA_PRIVATE_KEY_PATH", "secrets/dev/api_rsa_private.pem"), BASE_DIR)
)

# --- Rate limiting (LLD §3.10) -----------------------------------------------------------------------------

RATE_LIMIT_ENABLED = _env.read_bool("RATE_LIMIT_ENABLED", default=True)
RATE_LIMIT_POLICIES = {
    "default": {"limit": 120, "window_seconds": 60, "fail_open": True},
    "search": {"limit": 60, "window_seconds": 60, "fail_open": True},
    "catalog_write": {"limit": 30, "window_seconds": 60, "fail_open": True},
    "booking_write": {"limit": 20, "window_seconds": 60, "fail_open": True},
    "payment": {"limit": 10, "window_seconds": 60, "fail_open": False},  # never charge unthrottled
    "encrypted": {"limit": 300, "window_seconds": 60, "fail_open": True},  # per IP, before any RSA work
}
TRUSTED_PROXY_COUNT = _env.read_int("TRUSTED_PROXY_COUNT", 0, minimum=0)
DEV_TOKENS_ALLOW_ADMIN = _env.read_bool("DEV_TOKENS_ALLOW_ADMIN", default=False)

# --- Booking rules -----------------------------------------------------------------------------------------

BOOKING_HOLD_TTL_SECONDS = _env.read_int("BOOKING_HOLD_TTL_SECONDS", 900, minimum=1)
INVENTORY_HORIZON_DAYS = _env.read_int("INVENTORY_HORIZON_DAYS", 365, minimum=1)
MAX_STAY_NIGHTS = _env.read_int("MAX_STAY_NIGHTS", 30, minimum=1)

# --- Django internals --------------------------------------------------------------------------------------

APPEND_SLASH = False
USE_TZ = True
TIME_ZONE = _BUSINESS_TIME_ZONE.key
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = _build_logging_config(_env.read_choice("LOG_LEVEL", "INFO", _LOG_LEVELS))
