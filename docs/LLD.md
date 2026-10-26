# Hotel Booking Service — Low-Level Design (LLD)

Companion to [HLD.md](HLD.md). Names, signatures, tables, codes and routes here are **binding** for the implementation. Revision 2 folds in an independent design review covering brief coverage, consistency, concurrency, Django feasibility and security.

## 1. Repository layout

```text
hotel-booking-service/
├── manage.py
├── hotel_booking_service/            # project package (like loan_origination_service/)
│   ├── __init__.py
│   ├── settings.py                   # from hotel_commons.settings.base_settings import *; INSTALLED_APPS += 6 apps
│   ├── test_settings.py              # fakeredis, static test keys, ENVIRONMENT=local
│   ├── urls.py                       # §10.1 — every route mounted here once
│   ├── asgi.py
│   └── wsgi.py
├── catalog/  inventory/  pricing/  discovery/  booking/  payment/     # Django apps, layout §1.1
├── libs/hotel-commons/               # shared library — pip install -e libs/hotel-commons
│   ├── pyproject.toml
│   ├── hotel_commons/                # §3
│   └── tests/
├── tests/                            # cross-app tests
│   ├── architecture/test_module_boundaries.py      (optional, built last)
│   ├── integration/test_booking_flow_api.py
│   └── integration/test_inventory_concurrency.py
├── secrets/dev/api_rsa_private.pem   # DEV-ONLY key for local runs; refused when ENVIRONMENT != local
├── docs/HLD.md  docs/LLD.md
├── scripts/bootstrap_env.py          # optional: generate fresh local keys into .env + secrets/
├── scripts/encrypted_request.py      # demo client for API payload encryption
├── conftest.py  pytest.ini  .coveragerc
├── requirements.txt  requirements-dev.txt
├── Dockerfile  docker-compose.yml  local_run.sh  run-tests.sh
├── .env.example                      # DEV-ONLY values, used by docker compose
├── .gitignore  .dockerignore
└── README.md                         # §16
```

**Pinned dependencies.** `requirements.txt`:

```text
Django==5.2.*
djangorestframework==3.16.*
drf-spectacular>=0.28,<0.30
psycopg[binary]==3.2.*
redis>=5.2,<7
PyJWT==2.10.*
cryptography>=44,<47
uuid6==2025.0.1
gunicorn==23.*
-e ./libs/hotel-commons
```

`requirements-dev.txt` adds `pytest==8.*`, `pytest-django==4.9.*` and `fakeredis[lua]>=2.26`.

### 1.1 App layout (booking shown; apps omit folders they don't need)

```text
booking/
├── __init__.py
├── apps.py                 # ready(): register IDOR resolvers and event handlers
├── admin.py
├── constants.py
├── enums.py                # BookingStatus, BookingType, BookingTrigger, BookingRefundStatus
├── status_code.py          # this app's StatusCode catalogue
├── container.py            # composition root: builds services with concrete adapters
├── urls.py
├── domain/                 # pure Python — no Django imports
├── interfaces/             # ABCs: repositories.py, ports.py
├── models/                 # Django ORM models: Common / Main / Log
├── repositories/           # Django implementations + mappers (model <-> domain)
├── adapters/               # port implementations over other apps' public APIs
├── services/               # use cases, commands.py, settings.py
├── public/                 # facade, DTOs, events, enums — the only cross-app import surface
├── serializers/
├── views/
├── management/commands/
├── migrations/
└── tests/                  # fakes.py (in-memory repos/ports), unit and API tests
```

## 2. Conventions (binding for every app)

1. **Python 3.13** with type hints. Entities are `@dataclass(slots=True)`; value objects, commands and DTOs are `@dataclass(frozen=True, slots=True)`. Django model instances never leave `repositories/`.
2. **Dependency injection.** Services receive dependencies through the constructor. `container.py` is the only place that picks implementations. Factories in `public/api.py` (`get_<app>_api()`) delegate to the container.
3. **Unit of work.** Every use case runs in a `UnitOfWork` (Django: `transaction.atomic()`), and nested use cases become savepoints. `ATOMIC_REQUESTS = False`, so an outcome a service commits is never rolled back by a request-level transaction.
4. **Repository writes.**
   - `add(entity)` builds a new model instance and calls `instance.save(force_insert=True)`.
   - `save(entity)` never constructs a model for an existing id. It loads `row = Model.objects.get(pk=entity.id)` inside the current transaction (the row is already locked by `get_for_update` where relevant), copies the mapped fields with `mapper.apply(entity, row)`, and calls `row.save(update_fields=[...mapped field names])`.
   - `AddCommonField.save` then stamps `updation_*`, fills the PII companions, and appends the log row.
   - A repository never calls `save()` on a model it did not load.
5. **Constraint violations.** A repository statement that can violate a constraint runs inside its own `with transaction.atomic():`. The repository catches `IntegrityError` *outside* that inner block, then raises the domain error, so the enclosing transaction stays usable.
6. **Errors.** Raise `AppError` subclasses with a `StatusCode` and named params, e.g. `raise Conflict(INVALID_STATE_TRANSITION, action="pay", state=booking.status.name)`.
7. **Outcomes vs errors.** When a state change must persist and the client must still get a non-2xx (payment declined, booking expired), the service **returns** an outcome and the view answers with `api_response(data, http_status=..., status=...)`. Services never raise after a state change they need committed.
8. **Logging.** `logger = logging.getLogger(__name__)`. Services log **ids only**, never commands or entities. Fields holding card data, PII or method details are declared `field(repr=False)`. `PiiRedactionFilter` is the safety net.
9. **Time.** Inject a `Clock`. Timestamps are timezone-aware UTC; stay dates are `date`; check-in instants use `Asia/Kolkata`.
10. **Money is integer paisa end to end.** `Money(paisa: int)` in the domain; `BIGINT *_paisa` columns in Postgres; no floats anywhere. Conversion happens **only** in the serializer: `HotelAmountField` deserialises rupees (`"4800.50"`) to `480050` and serialises paisa back to `"4800.50"`. Every rounding is `ROUND_HALF_UP` to a whole paisa. Response objects carry a sibling `currency: "INR"`.
11. **Enums are `SMALLINT` in the database.** `hotel_commons.enum.Enum` with integer values, persisted by the model `EnumField` (int ↔ member). On the wire they are **member names**: `HotelEnumField(enum)` deserialises `"PREPAID"` to the member, serialises the member (or a raw int) to `"PREPAID"`, and rejects anything else listing the allowed names. The integer never leaks into the API, so reordering or renaming labels is safe.
12. **Serializers use the custom `Hotel*Field` classes (§3.14) — never bare DRF fields.** Field-level security is declared on the field (`restrict_roles=[Role.ADMIN]`, `read_roles`, `subject_attr`, `mask_as`), exactly as the reference LOS serializers declare `restrict_roles` on `Kuhoo*Field`s.
13. **One database, one schema per context (§3.15).** Every `db_table` is `schema_table("<context>", "<table>")`.
14. **Routes.** No trailing slashes anywhere (`APPEND_SLASH = False`). Path ids use `<uuid:...>` converters, so a malformed id gets a 404 envelope.

## 3. Shared library — `hotel_commons`

### 3.1 Package tree and import rules

```text
hotel_commons/
├── __init__.py              # Service registry: Service(name=...) — like kuhoo
├── apps.py                  # HotelCommonsConfig; ready() imports api/schema.py (spectacular extension)
├── consts.py
├── enum.py                  # Enum with labels and int conversion
├── status_code.py           # StatusCode + common codes
├── exceptions.py            # AppError hierarchy (no Django imports)
├── context.py               # RequestContext over ContextVars
├── domain/                  # money.py, date_range.py, clock.py, unit_of_work.py
├── db/                      # unit_of_work.py (DjangoUnitOfWork), schema.py (schema_table, create_schema_operation)
├── serializers/             # fields.py (Hotel*Field), base.py (BaseHotelSerializer, BaseHotelModelSerializer)
├── models/                  # base.py, managers.py, log_model.py, fields.py, pii_companions.py
├── security/                # __init__.py (EMPTY, no re-exports), principal.py, pi_type.py, pii.py,
│                            # jwt_tokens.py, api_crypto.py, idor.py, field_level.py, permissions.py, client_ip.py
├── middlewares/             # request_context.py, exception_handling.py, api_encryption.py, authentication.py
├── api/                     # response.py, exception_handler.py, views.py, pagination.py, throttling.py,
│                            # authentication.py, fields.py, schema.py, system_views.py, error_views.py, urls.py
├── ratelimit/               # interfaces.py, redis_sliding_window.py
├── idempotency/             # interfaces.py, redis_store.py, decorators.py
├── events/                  # base.py, interfaces.py, in_process.py
├── redis/client.py
├── logging/                 # config.py, filters.py
├── settings/base_settings.py
├── management/commands/mint_token.py
└── testing/                 # clock.py, unit_of_work.py, events.py, stores.py, tokens.py
```

Import rules inside the library:
- `security/pi_type.py` imports only `hotel_commons.enum`.
- `security/pii.py` never imports `hotel_commons.models`.
- `models/pii_companions.py` imports `models.fields` and `security.pii`; `models/base.py` and `models/managers.py` import `pii_companions`.
- `domain/` imports only `exceptions` and `status_code`.

### 3.2 Status codes and exceptions

```python
# hotel_commons/status_code.py
@dataclass(frozen=True, slots=True)
class StatusCode:
    code: int
    message: str
    http_status: int = 400
    def render(self, **params) -> str:
        return self.message.format_map(_SafeDict(params))   # missing placeholder -> left as "{name}"

class _SafeDict(dict):
    def __missing__(self, key): return "{" + key + "}"

SERVICE_CODE = 21                                          # code = 21 MM EEE, e.g. 2105002

SUCCESS                   = StatusCode(2100000, "Success", 200)
VALIDATION_FAILED         = StatusCode(2100001, "Invalid request details", 400)
UNAUTHENTICATED           = StatusCode(2100002, "Authentication credentials are missing or invalid", 401)
TOKEN_EXPIRED             = StatusCode(2100003, "Token has expired", 401)
FORBIDDEN                 = StatusCode(2100004, "You are not allowed to perform this action", 403)
FIELD_ACCESS_DENIED       = StatusCode(2100005, "Field '{field}' cannot be set by your role", 403)
RESOURCE_NOT_FOUND        = StatusCode(2100006, "{resource} not found", 404)
RATE_LIMITED              = StatusCode(2100007, "Too many requests, retry after {retry_after}s", 429)
IDEMPOTENCY_KEY_REQUIRED  = StatusCode(2100008, "Idempotency-Key header is required", 400)
IDEMPOTENCY_KEY_INVALID   = StatusCode(2100009, "Idempotency-Key must be 8-128 characters of [A-Za-z0-9_-]", 400)
IDEMPOTENCY_IN_PROGRESS   = StatusCode(2100010, "A request with this Idempotency-Key is still in progress", 409)
IDEMPOTENCY_KEY_REUSED    = StatusCode(2100011, "Idempotency-Key was already used with a different request", 422)
PAYLOAD_NOT_ENCRYPTED     = StatusCode(2100012, "Payload should be encrypted", 400)
PAYLOAD_DECRYPTION_FAILED = StatusCode(2100013, "Encrypted payload could not be decrypted", 400)
SECURITY_ERROR            = StatusCode(2100014, "Please try again after some time", 500)
INTERNAL_ERROR            = StatusCode(2100015, "Please try again after some time", 500)
LOG_MODEL_NOT_FOUND       = StatusCode(2100016, "Invalid log model configured for {model}", 500)
METHOD_NOT_ALLOWED        = StatusCode(2100017, "Method {method} not allowed", 405)
NOT_FOUND                 = StatusCode(2100018, "Resource not found", 404)
```

```python
# hotel_commons/exceptions.py — pure Python
class AppError(Exception):
    def __init__(self, status_code: StatusCode, /, *, errors: Mapping[str, Any] | None = None, **params): ...
    # attributes: status_code, params, errors, message (= status_code.render(**params))
    @property
    def http_status(self) -> int: return self.status_code.http_status

# Readable aliases; the HTTP status always comes from the StatusCode.
class ValidationFailed(AppError): ...
class Unauthenticated(AppError): ...
class PaymentDeclined(AppError): ...
class Forbidden(AppError): ...
class NotFound(AppError): ...
class Conflict(AppError): ...
class BusinessRuleViolation(AppError): ...
class TooManyRequests(AppError): ...          # extra kw: retry_after_seconds (sets Retry-After)
class SecurityFailure(AppError): ...
class UpstreamFailure(AppError): ...          # e.g. payment gateway refund failure (502)
```

### 3.3 Principal and request context

```python
# hotel_commons/security/principal.py
class Role(StrEnum):
    GUEST = "GUEST"
    OWNER = "OWNER"
    ADMIN = "ADMIN"

@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    roles: frozenset[Role]
    is_authenticated: bool = True              # DRF compatibility
    def has_any_role(self, *roles: Role) -> bool: ...

ANONYMOUS = Principal(user_id="anonymous", roles=frozenset(), is_authenticated=False)
SYSTEM_ACTOR = "system"

# hotel_commons/context.py
class RequestContext:
    @staticmethod
    def bind(*, request_id: str | None = None, principal: Principal | None = None) -> list[Token]: ...
    @staticmethod
    def reset(tokens: list[Token]) -> None: ...
    @staticmethod
    def request_id() -> str | None: ...
    @staticmethod
    def principal() -> Principal: ...           # ANONYMOUS outside a request
    @staticmethod
    def actor() -> str: ...                     # principal.user_id, or SYSTEM_ACTOR if not authenticated
```

### 3.4 Domain kernel

```python
# hotel_commons/domain/money.py
@dataclass(frozen=True, slots=True)
class Money:
    paisa: int                       # minor units: ₹ × 100. int only (bool rejected); may be 0, never negative
    currency: str = "INR"
    # __post_init__: type/sign checks; currency must be "INR" -> else ValidationFailed(VALIDATION_FAILED, errors={...})
    @classmethod
    def zero(cls, currency: str = "INR") -> "Money": ...
    @classmethod
    def of_rupees(cls, rupees: Decimal | str | int) -> "Money": ...
    # Decimal(str(x)); more than 2 decimal places, NaN or negative -> ValidationFailed
    @property
    def rupees(self) -> Decimal: ...                         # Decimal(paisa) / 100, quantized 0.01
    def __add__(self, other: "Money") -> "Money": ...
    def __sub__(self, other: "Money") -> "Money": ...
    def times(self, factor: int | Decimal) -> "Money": ...   # paisa × factor, ROUND_HALF_UP to a whole paisa
    def percent(self, pct: Decimal) -> "Money": ...          # pct in 0..100, ROUND_HALF_UP
    def divide(self, n: int) -> "Money": ...                 # ROUND_HALF_UP (used for per-night averages)
    def is_positive(self) -> bool: ...
    # ordering (<, <=, >, >=) within a currency

# hotel_commons/domain/date_range.py
@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date            # exclusive; start >= end -> ValidationFailed(VALIDATION_FAILED, errors={"dates": "check_out must be after check_in"})
    @property
    def nights(self) -> int: ...
    def dates(self) -> tuple[date, ...]: ...
    def overlaps(self, other: "DateRange") -> bool: ...

# hotel_commons/domain/clock.py
class Clock(Protocol):
    def now(self) -> datetime: ...              # aware UTC
    def today(self) -> date: ...                # in Asia/Kolkata
class SystemClock: ...

# hotel_commons/domain/unit_of_work.py
class UnitOfWork(Protocol):
    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, *exc) -> bool | None: ...

# hotel_commons/db/unit_of_work.py
class DjangoUnitOfWork:                         # transaction.atomic(using=alias); re-entrant (savepoints)
    def __init__(self, using: str = "default"): ...
```

### 3.5 Models — `AddCommonField` and the log-model strategy

```python
# hotel_commons/models/base.py
class RecordStatus(Enum):
    INACTIVE = 0
    ACTIVE = 1

class CommonFieldsMeta(ModelBase):
    """Adds `request_id` to every concrete model whose class name ends with 'Log' (case-sensitive)."""
    def __new__(mcs, name, bases, attrs, **kwargs):
        meta = attrs.get("Meta")
        if name.endswith("Log") and not getattr(meta, "abstract", False) and "request_id" not in attrs:
            attrs["request_id"] = models.CharField(max_length=64, null=True, blank=True)
        return super().__new__(mcs, name, bases, attrs, **kwargs)

class AddCommonField(models.Model, metaclass=CommonFieldsMeta):
    status = EnumField(RecordStatus, default=RecordStatus.ACTIVE)
    creation_date = models.DateTimeField(auto_now_add=True)
    creation_by = models.TextField()
    updation_date = models.DateTimeField(null=True, blank=True)
    updation_by = models.TextField(null=True, blank=True)

    objects = CustomUpdateManager.as_manager()      # default manager
    objects_active = ActiveQuerySetManager()        # status == ACTIVE only

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        with transaction.atomic(using=kwargs.get("using")):
            stamp_common_fields(self, kwargs)       # decides insert vs update with self._state.adding, NEVER self.pk
            secure_pii_companions(self, kwargs)     # masked_<field> / <field>_hash from plaintext; widens update_fields
            super().save(*args, **kwargs)
            add_log_model(self)                     # no-op unless a log model is registered
```

`stamp_common_fields`:
- Insert (`_state.adding`): `creation_by = RequestContext.actor()`.
- Update: `updation_by = actor`, `updation_date = now`.
- If `update_fields` was given, it is widened with `updation_by` and `updation_date`.
- Log models have no `updation_*` columns; the stamp skips fields the model doesn't have.

```python
# hotel_commons/models/log_model.py
class LogModelRegistry:
    """Maps 'app_label.Model' -> 'app_label.ModelLog'; resolved lazily with apps.get_model and cached."""
    @classmethod
    def register(cls, model: str, log_model: str) -> None: ...
    @classmethod
    def log_model_for(cls, model_cls: type[models.Model]) -> type[models.Model] | None: ...
    # registered but unresolvable -> SecurityFailure(LOG_MODEL_NOT_FOUND, model=...)

def snapshot_values(instance: models.Model, log_cls: type[models.Model]) -> dict[str, Any]: ...
    # attname -> value for every concrete field the log model shares with the instance, except
    # id, creation_date, creation_by, updation_date, updation_by, request_id, log

def add_log_model(instance: models.Model) -> models.Model | None: ...
    # log_cls(**snapshot_values(...), log=instance, request_id=RequestContext.request_id()).save(force_insert=True)

def create_bulk_log_entries(log_cls: type[models.Model], instances: Iterable[models.Model]) -> None: ...
```

```python
# hotel_commons/models/managers.py
class CustomUpdateManager(models.QuerySet):
    @staticmethod
    def set_logModel(logModel: str, model: str) -> None:            # name kept from the reference library
        LogModelRegistry.register(model=model, log_model=logModel)

    def update(self, **kwargs) -> int: ...
        # atomic: capture pks; add updation_by/updation_date; fill PII companions for encrypted fields in kwargs;
        # super().update(); if a log model is registered: re-read rows by pk -> create_bulk_log_entries()
    def bulk_create(self, objs, **kwargs) -> list: ...
        # stamps + companions; log rows only when ignore_conflicts is falsy (skipped rows would get phantom logs)
    def bulk_update(self, objs, fields, batch_size=None) -> int: ...    # stamps + companions + log rows
    def get_or_not_found(self, status_code: StatusCode, /, **lookup) -> models.Model: ...
        # DoesNotExist -> NotFound(status_code, resource=self.model.__name__)

class ActiveQuerySetManager(models.Manager.from_queryset(CustomUpdateManager)):
    def get_queryset(self): return super().get_queryset().filter(status=RecordStatus.ACTIVE)
```

**Model triad rules**:
1. `CommonX(AddCommonField)` is abstract and holds only the business columns. FKs declared in `CommonX` use `related_name="+"`.
2. `X(CommonX)` calls `CustomUpdateManager.set_logModel(logModel="app.XLog", model="app.X")` in its class body. It declares `db_table = schema_table("<schema>", "<x>")`, indexes and constraints, and overrides an FK when a reverse accessor is needed.
3. `XLog(CommonX)` adds `log = models.ForeignKey("app.X", on_delete=models.RESTRICT, related_name="logs")`, sets `updation_date = None` and `updation_by = None`, and uses `db_table = schema_table("<schema>", "<x>_log")` (§3.15).
4. Unique constraints and indexes live on concrete classes only, so snapshots never collide.
5. `id = models.UUIDField(primary_key=True, default=uuid7, editable=False)` is declared in `CommonX` (`from uuid6 import uuid7`). Log rows get a fresh id.

```python
# Example — booking/models/booking_models.py
class CommonBooking(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    guest_id = models.CharField(max_length=64)
    # ... business columns (§8.7)
    class Meta:
        abstract = True

class Booking(CommonBooking):
    CustomUpdateManager.set_logModel(logModel="booking.BookingLog", model="booking.Booking")
    class Meta:
        db_table = schema_table("booking", "booking")      # -> 'booking"."booking'
        constraints = [models.CheckConstraint(condition=Q(check_out__gt=F("check_in")), name="booking_dates_valid")]

class BookingLog(CommonBooking):
    log = models.ForeignKey(Booking, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None
    class Meta:
        db_table = schema_table("booking", "booking_log")
        indexes = [models.Index(fields=["log", "creation_date"])]
```

### 3.6 PII at rest

```python
# hotel_commons/security/pi_type.py
class PiType(Enum):
    NAME = 1; EMAIL = 2; PHONE = 3; UPI_VPA = 4; OTHER = 9

# hotel_commons/security/pii.py
class Ciphertext(str):
    """Marker type: only PiiCipher.encrypt returns it."""

class PiiCipher:
    PREFIX = "ENC_"
    def __init__(self, keys: Mapping[str, bytes], active_key_id: str): ...  # 32-byte keys; key id ^[A-Za-z0-9]{1,16}$
    def encrypt(self, plaintext: str, *, aad: str) -> Ciphertext: ...
        # "ENC_<kid>:" + urlsafe_b64(nonce(12, os.urandom) || AESGCM ciphertext+tag), AAD bound
    def decrypt(self, value: str, *, aad: str) -> str: ...           # SecurityFailure(SECURITY_ERROR) on tamper/unknown key
    @staticmethod
    def looks_encrypted(value: object) -> bool: ...                   # prefix check, used by the DB read path only

class BlindIndexer:
    def __init__(self, key: bytes): ...
    def digest(self, value: str, pi_type: PiType) -> str: ...
    # hex HMAC-SHA256 of the normalised value: EMAIL strip+lower; PHONE digits, last 10; else strip

class Masker:
    @staticmethod
    def mask(value: str | None, pi_type: PiType) -> str | None: ...
    # NAME "Rahul Sharma" -> "R**** S*****"; EMAIL "rahul@gmail.com" -> "r****@gmail.com";
    # PHONE "9876543210" -> "******3210"; UPI_VPA "rahul@okaxis" -> "r****@okaxis"; OTHER -> "****" + last 4

def get_pii_cipher() -> PiiCipher: ...        # lru_cache from settings PII_ENCRYPTION_KEYS / PII_ACTIVE_KEY_ID
def get_blind_indexer() -> BlindIndexer: ...  # lru_cache from settings PII_BLIND_INDEX_KEY
```

```python
# hotel_commons/models/fields.py
class EnumField(models.PositiveSmallIntegerField):
    def __init__(self, enum: type[Enum], *args, **kwargs): ...  # db_comment auto from members (unless given)
    # from_db_value -> enum member; to_python -> member; get_prep_value(member | int) -> int; deconstruct keeps enum

class EncryptedTextField(models.TextField):
    """PII column: AES-256-GCM ciphertext at rest, plaintext in Python."""
    def __init__(self, *args, pi_type: PiType, **kwargs): ...
    def deconstruct(self): ...                                  # adds kwargs["pi_type"]
    def get_prep_value(self, value): ...
    # None -> None; Ciphertext -> unchanged; any other value -> encrypt(str(value), aad=f"{pi_type.name}|{self.name}")
    def from_db_value(self, value, expression, connection): ...  # decrypt with the same AAD -> plain str
    def to_python(self, value): ...                             # returns value unchanged (never decrypts user input)
    def get_lookup(self, lookup_name): ...                      # only "isnull"; otherwise FieldError("query <field>_hash")

class BlindIndexField(models.CharField):
    def __init__(self, *args, **kwargs): ...
    # setdefault max_length=64, null=True, db_index=True, editable=False
    def deconstruct(self): ...                                  # drops those four defaults
```

The AAD is `"<PI type>|<column name>"`. It is identical on the live and log tables (same column names), and it stops a ciphertext being copied from one column into another. User input that happens to start with `ENC_` is just a string, so it gets encrypted like anything else.

```python
# hotel_commons/models/pii_companions.py
def secure_pii_companions(instance: models.Model, save_kwargs: dict) -> None: ...
# For every field f with getattr(f, "pi_type", None) set (duck typing):
#   plaintext = getattr(instance, f.attname); if model has "masked_<name>" -> Masker.mask(...);
#   if model has "<name>_hash" -> BlindIndexer.digest(...); add both to update_fields when present.
def companion_values_for_update(model_cls, kwargs: dict) -> dict: ...   # same, for QuerySet.update(**kwargs)
```

### 3.7 Security

```python
# hotel_commons/security/jwt_tokens.py
class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...

class JwtTokenService:
    def __init__(self, *, signing_key: bytes, issuer: str, audience: str,
                 ttl_seconds: int = 3600, leeway_seconds: int = 30): ...
    # HS256 only; signing_key must be >= 32 bytes (ValueError otherwise)
    def issue(self, *, user_id: str, roles: Iterable[Role], ttl_seconds: int | None = None) -> str: ...
    def verify(self, token: str) -> Principal: ...
    # jwt.decode(token, key, algorithms=["HS256"], audience=..., issuer=..., leeway=...,
    #            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "roles"]})
    # ExpiredSignatureError -> Unauthenticated(TOKEN_EXPIRED); any other jwt error -> Unauthenticated(UNAUTHENTICATED)
    # sub must match ^[A-Za-z0-9_.@-]{1,64}$; roles must be a list; unknown roles dropped; no known role -> UNAUTHENTICATED

def get_token_service() -> JwtTokenService: ...    # lru_cache from settings
# claims: {"sub", "roles": [...], "iss", "aud", "iat", "nbf", "exp", "jti"}
```

```python
# hotel_commons/security/api_crypto.py — hybrid RSA-OAEP-256 + AES-256-GCM
class HybridPayloadCipher:                                     # server side
    def __init__(self, private_key_pem: bytes): ...            # RSA >= 3072 bits
    def public_key_pem(self) -> str: ...
    def unwrap_key(self, wrapped_key_b64: str) -> bytes: ...   # OAEP(mgf=MGF1(SHA256), algorithm=SHA256, label=None) -> 32 bytes
    def decrypt(self, aes_key: bytes, payload_b64: str, *, aad: str) -> bytes: ...
    def encrypt(self, aes_key: bytes, plaintext: bytes, *, aad: str) -> str: ...   # fresh os.urandom(12) nonce every call

class HybridPayloadClient:                                     # tests and scripts/encrypted_request.py
    def __init__(self, server_public_key_pem: bytes): ...
    def encrypt_request(self, method: str, path: str, body: Mapping | None) -> EncryptedRequest: ...
    def decrypt_response(self, request: EncryptedRequest, response_body: Mapping) -> dict: ...

@dataclass(frozen=True)
class EncryptedRequest:
    headers: dict[str, str]; body: dict[str, str] | None; aes_key: bytes = field(repr=False)
    method: str; path: str; timestamp: int

def get_payload_cipher() -> HybridPayloadCipher: ...           # lazy; reads API_RSA_PRIVATE_KEY_PATH on first use
```

Wire format (v1):

| Item | Value |
|---|---|
| Request headers | `X-Payload-Encryption: v1`, `X-Encrypted-Key: <b64 RSA-OAEP(aes_key)>`, `X-Payload-Timestamp: <unix seconds>` (±300 s) |
| Request body | `{"payload": "<b64(nonce ‖ ciphertext ‖ tag)>"}`; GET/DELETE may have an empty body |
| Request AAD | `v1\|req\|{METHOD}\|{path}\|{ts}` |
| Response | header `X-Payload-Encryption: v1`, body `{"payload": ...}`, AAD `v1\|res\|{METHOD}\|{path}\|{ts}`, same AES key, new nonce |
| Replay guard | `SET hb:enc:v1:{sha256(X-Encrypted-Key)} 1 NX EX 300`; already present → 400 `PAYLOAD_DECRYPTION_FAILED` |

```python
# hotel_commons/security/idor.py
@dataclass(frozen=True, slots=True)
class Ownership:
    guest_user_ids: frozenset[str] = frozenset()
    owner_user_ids: frozenset[str] = frozenset()

class OwnershipResolver(ABC):
    @abstractmethod
    def resolve(self, resource_id: UUID) -> Ownership | None: ...   # None = no such (active) resource; reads objects_active

class OwnershipRegistry:
    def register(self, resource: str, resolver: OwnershipResolver) -> None: ...
    def resolve(self, resource: str, resource_id: UUID) -> Ownership | None: ...   # no cache (straight from Postgres)

ownership_registry = OwnershipRegistry()

Relation = Literal["guest", "owner"]

@dataclass(frozen=True, slots=True)
class IdorRule:
    resource: str                                     # "booking", "property", "owner_account", "payment"
    key: str                                          # path kwarg / body field / query param
    source: Literal["path", "body", "query"] = "path"
    relations: frozenset[Relation] = frozenset({"guest", "owner"})

class IdorGuard:
    def __init__(self, registry: OwnershipRegistry = ownership_registry): ...
    def check(self, principal: Principal, rules: Sequence[IdorRule], *,
              path_kwargs: Mapping, body: Mapping, query: Mapping) -> None: ...
    # ADMIN bypasses. For each rule: read the value (absent -> skip; serializers enforce required fields);
    # parse with UUID(str(v)) (a list is checked element-wise); malformed, unknown, or not accessible ->
    # NotFound(RESOURCE_NOT_FOUND, resource=rule.resource)
    @staticmethod
    def can_access(principal: Principal, ownership: Ownership, relations: frozenset[Relation]) -> bool: ...
    # (GUEST in roles and "guest" in relations and uid in guest_user_ids) or
    # (OWNER in roles and "owner" in relations and uid in owner_user_ids)
```

```python
# hotel_commons/security/field_level.py — the engine behind the field kwargs of §3.14
class FieldLevelSecurityMixin:                     # mixed into BaseHotelSerializer / BaseHotelModelSerializer
    def to_internal_value(self, data): ...
    # for every field present in `data` (recursing into nested serializers and list children):
    # field.restrict_roles non-empty and caller has none of them -> Forbidden(FIELD_ACCESS_DENIED, field=<dotted name>)
    def to_representation(self, instance): ...
    # per field: readable = not field.read_roles or caller has one of them
    #            or (field.subject_attr and caller.user_id == str(getattr(instance, field.subject_attr)))
    # not readable -> Masker.mask(value, field.mask_as) if mask_as else the key is dropped
    # principal: self.context["request"].principal, else RequestContext.principal()
```

```python
# hotel_commons/security/permissions.py
class IsAuthenticatedPrincipal(BasePermission): ...
def require_roles(*roles: Role) -> type[BasePermission]: ...   # cached class factory; 403 FORBIDDEN on failure

# hotel_commons/security/client_ip.py
def client_ip(request) -> str: ...
# TRUSTED_PROXY_COUNT == 0 -> REMOTE_ADDR; else the hop at XFF.split(",")[-TRUSTED_PROXY_COUNT]
# (falls back to REMOTE_ADDR when there are too few hops); validated with ipaddress.ip_address
```

### 3.8 Middlewares (order in `base_settings.MIDDLEWARE`)

| # | Class | Behaviour |
|---|---|---|
| 1 | `django.middleware.security.SecurityMiddleware`, `django.middleware.common.CommonMiddleware` | Django defaults (`APPEND_SLASH = False`) |
| 2 | `hotel_commons.middlewares.request_context.RequestContextMiddleware` | Request id from `X-Request-ID` (≤ 64 chars `[A-Za-z0-9-]`) or a new uuid4 hex. Sets `request.request_id`, binds `RequestContext`, echoes `X-Request-ID`, and resets in `finally`. |
| 3 | `hotel_commons.middlewares.exception_handling.ExceptionHandlingMiddleware` | Uses `process_exception(request, exc)`. `AppError` → `JsonResponse(error_body(...), status=exc.http_status)`. Anything else → logged, then a 500 `INTERNAL_ERROR` JsonResponse. Never builds a DRF `Response`. |
| 4 | `hotel_commons.middlewares.api_encryption.ApiEncryptionMiddleware` | `API_ENCRYPTION_MODE` is `off`, `optional` or `enforced`. Exact-path bypass set `API_ENCRYPTION_BYPASS_PATHS`. Before any RSA work it rate-limits by client IP (policy `encrypted`, 300/60 s). With the v1 headers it checks the timestamp, unwraps the key, checks for replay, decrypts and replaces `request._body` (and `CONTENT_TYPE`), and keeps the key on `request`. In `enforced` mode any non-bypass request without the headers gets 400 `PAYLOAD_NOT_ENCRYPTED`. It encrypts the response when the request was encrypted. Its own errors are returned as `JsonResponse`, never raised. |
| 5 | `hotel_commons.middlewares.authentication.JWTAuthenticationMiddleware` | `Authorization: Bearer <jwt>` → `request.principal`; no header → `ANONYMOUS`. An invalid token returns a 401 `JsonResponse` immediately. Rebinds `RequestContext` with the principal. |

`hotel_commons/api/error_views.py`: `not_found(request, exception)` and `server_error(request)` return envelope `JsonResponse`s. The project's `urls.py` sets `handler404` and `handler500` to them.

### 3.9 API helpers

```python
# hotel_commons/api/response.py
def api_response(data: Any = None, *, http_status: int = 200, status: StatusCode = SUCCESS,
                 message: str | None = None, headers: Mapping[str, str] | None = None) -> Response: ...
# body: {"status": status.code, "message": message or status.message, "data": data, "request_id": ...}
def error_body(status: StatusCode, message: str, errors: Mapping | None = None, data: Any = None) -> dict: ...
# {"status", "message", "errors", "data", "request_id"}

# hotel_commons/api/exception_handler.py   (REST_FRAMEWORK["EXCEPTION_HANDLER"])
def envelope_exception_handler(exc, context) -> Response | None: ...
# AppError -> error body + exc.http_status (Retry-After for TooManyRequests); DRF ValidationError -> 400
# VALIDATION_FAILED with field errors; NotAuthenticated/AuthenticationFailed -> 401; PermissionDenied -> 403;
# Http404/NotFound -> 404 NOT_FOUND; Throttled -> 429 RATE_LIMITED (+Retry-After); MethodNotAllowed -> 405;
# anything else -> None (the middleware answers 500)

# hotel_commons/api/authentication.py
class PrincipalAuthentication(BaseAuthentication):
    def authenticate(self, request): ...   # (request._request.principal, None) if authenticated else None
    def authenticate_header(self, request): return "Bearer"

# hotel_commons/api/views.py
class BaseViewSet(GenericViewSet):
    authentication_classes = [PrincipalAuthentication]
    permission_classes = [IsAuthenticatedPrincipal]
    throttle_classes = [RedisRateLimitThrottle]
    permission_classes_by_action: ClassVar[Mapping[str, Sequence[type[BasePermission]]]] = {}
    throttle_scope_by_action: ClassVar[Mapping[str, str]] = {}         # default "default"
    idor_rules: ClassVar[Mapping[str, Sequence[IdorRule]]] = {}        # keyed by action name
    def get_permissions(self): ...
    def get_throttle_scope(self) -> str: ...
    def initial(self, request, *args, **kwargs): ...
    # format negotiation/versioning as DRF does, then:
    # perform_authentication -> check_throttles -> check_permissions -> IdorGuard().check(...)
    def finalize_response(self, request, response, *args, **kwargs): ...   # X-RateLimit-Limit / -Remaining

# serializer fields live in hotel_commons/serializers/ (§3.14)

# hotel_commons/api/pagination.py
@dataclass(frozen=True) class PageRequest: page: int = 1; page_size: int = 20     # page_size max 50
def page_data(items: list, *, page: int, page_size: int, total: int) -> dict: ...
# {"items": [...], "page": n, "page_size": n, "total": n}

# hotel_commons/api/schema.py
class PrincipalAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "hotel_commons.api.authentication.PrincipalAuthentication"
    name = "BearerAuth"
    def get_security_definition(self, auto_schema): return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
def envelope(serializer_or_none, name: str) -> Serializer: ...   # inline_serializer {status, message, request_id, data}
ERROR_ENVELOPE: Serializer                                       # {status, message, errors, data, request_id}

# hotel_commons/api/system_views.py — APIView subclasses, permission AllowAny
class HealthView(APIView): ...        # GET: SELECT 1 + Redis PING -> {"database": "ok", "redis": "ok"|"down"}; throttle_classes = []
class PublicKeyView(APIView): ...     # GET: {"algorithm": "RSA-OAEP-256+A256GCM", "public_key_pem": "..."}
class DevTokenView(APIView): ...      # POST {"user_id", "roles": [...]} -> {"token", "expires_in"};
                                      # ADMIN refused unless DEV_TOKENS_ALLOW_ADMIN; route exists only when ENVIRONMENT == "local"

# hotel_commons/api/urls.py — mounted at hotel/<v>/
# health, security/public-key, dev/tokens (local only), schema (SpectacularAPIView), docs (SpectacularSwaggerView)
```

Every view action carries `@extend_schema(request=<serializer>, responses={<code>: envelope(<serializer>, "<Name>"), 4xx: ERROR_ENVELOPE})`.

### 3.10 Rate limiter

```python
# hotel_commons/ratelimit/interfaces.py
@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    scope: str
    limit: int
    window_seconds: int
    fail_open: bool = True

@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

class RateLimiter(ABC):
    @abstractmethod
    def hit(self, subject: str, policy: RateLimitPolicy) -> RateLimitDecision: ...

# hotel_commons/ratelimit/redis_sliding_window.py
class RedisSlidingWindowRateLimiter(RateLimiter):
    def __init__(self, redis_client): ...
    # register_script once; key = f"hb:rl:v1:{policy.scope}:{subject}"; member = f"{time_ns()}-{uuid4().hex[:8]}"
    # RedisError: policy.fail_open -> allowed (WARNING log); else -> not allowed, retry_after 5

# hotel_commons/api/throttling.py
class RedisRateLimitThrottle(BaseThrottle):
    def allow_request(self, request, view) -> bool: ...
    # if not settings.RATE_LIMIT_ENABLED: True
    # scope = view.get_throttle_scope() if hasattr(view, "get_throttle_scope") else getattr(view, "throttle_scope", None) or "default"
    # policy = RATE_LIMIT_POLICIES[scope] (fallback "default")
    # subject = "u:" + principal.user_id if authenticated else "ip:" + client_ip(request)
    # stores the decision on request.rate_limit_decision
    def wait(self) -> float | None: ...
```

```lua
-- KEYS[1] = key; ARGV[1] = limit; ARGV[2] = window seconds; ARGV[3] = unique member
local limit = tonumber(ARGV[1])
local window_us = tonumber(ARGV[2]) * 1000000
local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now_us - window_us)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now_us, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], math.ceil(window_us / 1000))
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, 0, math.ceil((tonumber(oldest[2]) + window_us - now_us) / 1000000)}
```

If `fakeredis[lua]` cannot run `TIME` inside a script, pass `now_us` from Python as `ARGV[4]` instead and note it in the README.

`RATE_LIMIT_POLICIES`:

| Scope | Limit / window | Fail open |
|---|---|---|
| `default` | 120 / 60 s | yes |
| `search` | 60 / 60 s | yes |
| `catalog_write` | 30 / 60 s | yes |
| `booking_write` | 20 / 60 s | yes |
| `payment` | 10 / 60 s | **no** |
| `encrypted` (middleware, per IP) | 300 / 60 s | yes |

### 3.11 Idempotency

```python
# hotel_commons/idempotency/interfaces.py
@dataclass(frozen=True, slots=True)
class StoredResponse:
    http_status: int
    body: dict

@dataclass(frozen=True, slots=True)
class BeginOutcome:
    kind: Literal["started", "in_progress", "replay", "mismatch"]
    response: StoredResponse | None = None

class IdempotencyStore(ABC):
    @abstractmethod
    def begin(self, key: str, fingerprint: str, lock_ttl_seconds: int) -> BeginOutcome: ...
    @abstractmethod
    def complete(self, key: str, fingerprint: str, response: StoredResponse, ttl_seconds: int) -> None: ...
    @abstractmethod
    def release(self, key: str) -> None: ...

# hotel_commons/idempotency/redis_store.py
class RedisIdempotencyStore(IdempotencyStore): ...
# Value = PiiCipher.encrypt(json.dumps(record, cls=rest_framework.utils.encoders.JSONEncoder), aad="idem") —
# the stored response may contain guest PII, so it is encrypted at rest.
# begin: SET key <IN_PROGRESS record> NX EX lock_ttl, else GET; complete: SET key <COMPLETED record> EX ttl; release: DEL

# hotel_commons/idempotency/decorators.py
def idempotent(scope: str, *, required: bool = True, ttl_seconds: int = 86_400, lock_ttl_seconds: int = 60): ...
```

Decorator algorithm, applied to a DRF view method:
1. Read `Idempotency-Key`.
   - Missing and `required` → 400 `IDEMPOTENCY_KEY_REQUIRED`. Missing and optional → run the view normally.
   - Format other than `^[A-Za-z0-9_-]{8,128}$` → 400 `IDEMPOTENCY_KEY_INVALID`.
   - Set `request.idempotency_key`.
2. Store key `hb:idem:v1:{scope}:{principal.user_id}:{key}`. Fingerprint = `HMAC-SHA256(IDEMPOTENCY_FINGERPRINT_KEY, f"{METHOD}|{path}|{canonical JSON of request.data}")`.
3. `begin`:
   - `RedisError` → log WARNING and run the view without the Redis layer; the Postgres unique keys still apply.
   - `replay` → return the stored response with header `Idempotent-Replayed: true`.
   - `in_progress` → 409.
   - `mismatch` → 422.
4. `started` → run the view.
   - An `AppError` it raises becomes a response via `envelope_exception_handler`.
   - Responses below 500 (returned or raised) → `complete()` and return.
   - 5xx or a non-`AppError` exception → `release()`, then return or re-raise.
   - A `RedisError` from `complete` or `release` is logged and ignored.

Scopes: `booking_create` (optional), `booking_pay` (required), `booking_cancel` (optional), `catalog_write` (optional).

### 3.12 Events

```python
# hotel_commons/events/base.py
@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    event_id: str = field(default_factory=lambda: str(uuid7()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

# hotel_commons/events/interfaces.py
class EventPublisher(ABC):
    @abstractmethod
    def publish(self, event: DomainEvent) -> None: ...

# hotel_commons/events/in_process.py
class InProcessEventBus(EventPublisher):
    def subscribe(self, event_type: type[DomainEvent], handler: Callable[[DomainEvent], None]) -> None: ...
    def publish(self, event: DomainEvent) -> None: ...
    # synchronous, in subscription order, inside the caller's transaction; handler errors propagate (rollback)
event_bus = InProcessEventBus()
```

### 3.13 Redis client, logging, settings, commands, testing

- **`redis/client.py`**: `get_redis()` is lru-cached. `REDIS_URL` starting with `fakeredis://` → a process-wide `fakeredis.FakeRedis(decode_responses=True)`. Otherwise `Redis.from_url(..., decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5)`. Also `redis_key(*parts)`, prefixed `hb:`.
- **`logging/filters.py`**: `RequestIdFilter` adds `request_id` and `actor`. `PiiRedactionFilter` masks emails, 10-digit mobiles, 13–19 digit card numbers and `"cvv": "..."` values in `msg` and `args`. `logging/config.py` provides `build_logging_config(level)`.
- **`settings/base_settings.py`** reads env with these defaults:
  - INSTALLED_APPS core: `contenttypes`, `auth`, `staticfiles`, `rest_framework`, `drf_spectacular`, `hotel_commons`.
  - MIDDLEWARE as §3.8. `APPEND_SLASH = False`, `ATOMIC_REQUESTS = False`, `USE_TZ = True`, `TIME_ZONE = "Asia/Kolkata"`.
  - REST_FRAMEWORK: JSON renderer and parser only, `PrincipalAuthentication`, `IsAuthenticatedPrincipal`, `RedisRateLimitThrottle`, `envelope_exception_handler`, `drf_spectacular.openapi.AutoSchema`, `UNAUTHENTICATED_USER: None`.
  - DATABASES from `POSTGRES_*`, with `CONN_MAX_AGE = 60` and `CONN_HEALTH_CHECKS = True`.
  - SPECTACULAR_SETTINGS: title and version, `SERVE_PERMISSIONS: ["rest_framework.permissions.AllowAny"]`, `SERVE_AUTHENTICATION: []`, `COMPONENT_SPLIT_REQUEST: True`.
  - **`ENVIRONMENT` defaults to `production`.**
  - Fail fast with `ImproperlyConfigured` when: `JWT_SIGNING_KEY` is missing or under 32 bytes; any PII key isn't 32 bytes; `PII_ACTIVE_KEY_ID` isn't in the key ring; `PII_BLIND_INDEX_KEY` or `IDEMPOTENCY_FINGERPRINT_KEY` is missing; or the committed dev keys are used while `ENVIRONMENT != "local"`.
  - A tiny `.env` loader runs `os.environ.setdefault` for each line when a `.env` file exists.
- **`management/commands/mint_token.py`**: `python manage.py mint_token --sub guest-1 --roles GUEST [--ttl 3600]`.
- **`testing/`**: `FixedClock`, `NoopUnitOfWork`, `RecordingEventPublisher`, `InMemoryIdempotencyStore`, `InMemoryRateLimiter`, `make_token(user_id, *roles)`, `auth_header(user_id, *roles)`.

### 3.14 Custom serializer fields (`hotel_commons/serializers/`)

Same idea as the reference `kuhoo.serializers.fields` (`KuhooIntegerField(restrict_roles=[ADMIN])`, `EnumField(enum, label_name="name")`, `KuhooAmountField`): every serializer field is a `Hotel*Field`, carries its own access rule, and owns its wire ↔ internal conversion.

```python
# hotel_commons/serializers/fields.py
class HotelFieldMixin:
    """Common kwargs for every Hotel*Field:
       restrict_roles: Sequence[Role] = ()   roles allowed to SEND the field (empty = anyone)  -> 403 otherwise
       read_roles:     Sequence[Role] = ()   roles allowed to SEE the raw value (empty = everyone)
       subject_attr:   str | None = None     attribute on the serialised object naming the data subject,
                                             who may always see their own value (e.g. "guest_id")
       mask_as:        PiType | None = None  when not readable: mask with Masker, else drop the key
       help_text is extended with the access rule (like kuhoo's help_text_for_access_role)."""

class HotelCharField(HotelFieldMixin, serializers.CharField): ...          # trims; max_length enforced
class HotelIntegerField(HotelFieldMixin, serializers.IntegerField): ...    # rejects bool and floats
class HotelBooleanField(HotelFieldMixin, serializers.BooleanField): ...
class HotelUUIDField(HotelFieldMixin, serializers.UUIDField): ...
class HotelDateField(HotelFieldMixin, serializers.DateField): ...          # "YYYY-MM-DD" only
class HotelDateTimeField(HotelFieldMixin, serializers.DateTimeField): ...  # ISO-8601 with offset (UTC out)
class HotelTimeField(HotelFieldMixin, serializers.TimeField): ...          # "HH:MM"
class HotelEmailField(HotelFieldMixin, serializers.EmailField): ...        # lower-cased
class HotelPhoneField(HotelCharField): ...                                 # ^[6-9]\d{9}$
class HotelListField(HotelFieldMixin, serializers.ListField): ...
class HotelCsvListField(HotelListField): ...                               # query param "WIFI,POOL" -> ["WIFI", "POOL"]

class HotelEnumField(HotelFieldMixin, serializers.Field):
    """Enum converter. DB: SMALLINT via model EnumField. Wire: member name."""
    def __init__(self, enum: type[Enum], *, label_name: str = "name", **kwargs): ...
    def to_internal_value(self, data) -> Enum: ...
    # str name -> member; anything else (including ints) ->
    # ValidationFailed(VALIDATION_FAILED, errors={field: "must be one of A, B, C"})
    def to_representation(self, value: Enum | int) -> str: ...   # member or raw int -> getattr(member, label_name)
    # help_text "One of: A, B, C"; OpenAPI enum emitted through @extend_schema_field

class HotelAmountField(HotelFieldMixin, serializers.Field):
    """Money converter. DB and domain: integer paisa. Wire: rupees as a 2-dp string."""
    def __init__(self, *, min_rupees: Decimal | None = None, max_rupees: Decimal | None = None, **kwargs): ...
    def to_internal_value(self, data) -> int: ...
    # "4800.5" | "4800.50" | 4800 | Decimal -> 480050 via Decimal(str(data)) * 100. Floats are converted through
    # str(), never binary; > 2 dp, negative, NaN/inf or out of range -> ValidationFailed with the field name
    def to_representation(self, value: int | Money) -> str: ...  # 480050 or Money(480050) -> "4800.50"

class HotelEncryptedCharField(HotelCharField):
    """PII field. mask_as is required. Never logs the value; masked on output unless the read rule passes."""

# hotel_commons/serializers/base.py
class BaseHotelSerializer(FieldLevelSecurityMixin, serializers.Serializer): ...
class BaseHotelModelSerializer(FieldLevelSecurityMixin, serializers.ModelSerializer): ...
# Every request/response serializer extends one of these. Views pass context={"request": request}.
```

Usage (booking):

```python
class BookingCreateSerializer(BaseHotelSerializer):
    room_type_id = HotelUUIDField()
    check_in = HotelDateField()
    check_out = HotelDateField()
    rooms = HotelIntegerField(min_value=1, max_value=10, default=1)
    guests = HotelIntegerField(min_value=1, max_value=40)
    booking_type = HotelEnumField(BookingType, default=BookingType.PREPAID)
    guest_name = HotelEncryptedCharField(min_length=2, max_length=100, mask_as=PiType.NAME)
    guest_email = HotelEmailField()
    guest_phone = HotelPhoneField()
    on_behalf_of_user_id = HotelCharField(required=False, max_length=64, restrict_roles=[Role.ADMIN])

class BookingSerializer(BaseHotelSerializer):
    booking_id = HotelUUIDField(source="id")
    status = HotelEnumField(BookingStatus)
    total_amount = HotelAmountField(source="price.total")
    currency = HotelCharField(source="price.total.currency")
    guest_email = HotelEncryptedCharField(source="contact.email", read_roles=[Role.ADMIN],
                                          subject_attr="guest_id", mask_as=PiType.EMAIL)
    # ...
```

### 3.15 Database: one PostgreSQL database, one schema per bounded context

**Layout.** A single PostgreSQL 18 database, `hotel`, with one schema per context:

| Schema | Tables |
|---|---|
| `catalog` | owner_account (+log), property (+log), room_type (+log), amenity |
| `inventory` | allotment, room_night, hold (+log) |
| `discovery` | listing |
| `booking` | booking (+log) |
| `payment` | payment (+log), refund (+log) |
| `public` | Django internals only (migrations, contenttypes, auth) |

`pricing` owns no tables.

```python
# hotel_commons/db/schema.py
def schema_table(schema: str, table: str) -> str:
    """db_table value that Django quotes as "schema"."table" on PostgreSQL."""
    return f'{schema}"."{table}'

def create_schema_operation(schema: str) -> migrations.RunSQL:
    return migrations.RunSQL(f'CREATE SCHEMA IF NOT EXISTS "{schema}"', reverse_sql=migrations.RunSQL.noop)
```

- Each app's `0001_initial` migration begins with `create_schema_operation("<schema>")`; tables are created schema-qualified.
- Tests run migrations (no `--nomigrations`), so test databases get the schemas too.
- Index and constraint names are explicit and prefixed with the table, e.g. `booking_booking_status_hold_idx`.

**Why schemas, and why one connection.**
- A schema is the ownership boundary of a context and the unit of a future split: `pg_dump --schema=booking` moves it to its own database. There are no cross-schema foreign keys, so nothing has to be untangled.
- All schemas are reached through the single `default` alias, i.e. one connection per request. Cross-context use cases (create booking → hold inventory; pay → charge → confirm hold) therefore stay **one ACID transaction**.
- One Django alias per schema was rejected: Django transactions are per alias, so that would silently turn every cross-context use case into an unprotected distributed transaction.
- In production each context gets its own database role with `USAGE`/`CREATE` on its schema only (least privilege); the monolith's role is granted all of them. After a split, a service keeps its role and simply connects to its own database.

**Scalability built into the schema.**

| Concern | Decision |
|---|---|
| Keys | UUIDv7 primary keys: time-ordered (B-tree inserts stay right-leaning), globally unique (merge/shard friendly), unguessable |
| Types | Money `BIGINT` paisa; enums `SMALLINT` with `db_comment` listing values; dates `DATE`; instants `TIMESTAMPTZ` |
| Hot rows | `inventory.room_night` has one row per room type per night, so contention only happens on the same room type and night. The table uses `fillfactor = 80` so `reserved_rooms` updates stay HOT (no index churn), plus tighter autovacuum settings |
| Fail fast | The connection sets `lock_timeout = 2s` and `statement_timeout = 5s` (`OPTIONS["options"]`), so a contended booking returns 409/503 quickly instead of queueing |
| Audit growth | `*_log` tables are append-only with a BRIN index on `creation_date` (tiny, ideal for time-correlated data). When volume demands, they are range-partitioned monthly by `creation_date`; old partitions are detached and archived |
| Inventory growth | `inventory.room_night` grows by 365 rows per room type per year. Its partition key is `stay_date` (monthly ranges, past months archived); the unique key `(room_type_id, stay_date)` already contains it |
| Search reads | `discovery.listing` is a denormalised read model with a composite `(city_key, is_active)` index and GIN on `amenity_codes`. It can be served from a read replica (`DATABASE_REPLICA_*` + a router for the discovery app) or moved to OpenSearch without touching writers |
| Connections | `CONN_MAX_AGE = 60` + health checks locally; PgBouncer (transaction pooling) or Django's psycopg pool in production |
| Integrity in the DB | CHECKs (counters, dates, ratings, amounts > 0), UNIQUEs (idempotency keys, room-type names, contact-email hash), partial index for pending holds — invariants hold even if application code is wrong |

## 4. `catalog` app — properties and owners (module 01)

### 4.1 Enums (`catalog/public/enums.py`, re-exported by `catalog/enums.py`)

```python
class PropertyType(Enum):            HOTEL = 1; RESORT = 2; APARTMENT = 3; VILLA = 4; HOSTEL = 5; HOMESTAY = 6
class PricingStrategyCode(Enum):     STANDARD = 1; WEEKEND = 2; DYNAMIC = 3
class CancellationPolicyCode(Enum):  FLEXIBLE = 1; MODERATE = 2; STRICT = 3; NON_REFUNDABLE = 4
class AmenityCategory(Enum):         PROPERTY = 1; ROOM = 2
```

### 4.2 Domain (`catalog/domain/`)

```python
@dataclass(frozen=True, slots=True)
class Address:
    line1: str; locality: str; city: str; state: str; postal_code: str; country: str = "IN"
    @property
    def city_key(self) -> str: ...                 # city.strip().lower()

@dataclass(frozen=True, slots=True)
class ContactDetails:                              # PII; email format, phone ^[6-9]\d{9}$
    email: str = field(repr=False); phone: str = field(repr=False)

@dataclass(slots=True)
class OwnerAccount:
    id: UUID; display_name: str; owner_user_id: str; contact: ContactDetails; is_active: bool = True
    @classmethod
    def open(cls, *, display_name: str, owner_user_id: str, contact: ContactDetails) -> "OwnerAccount": ...

@dataclass(slots=True)
class RoomType:
    id: UUID; property_id: UUID; name: str; max_occupancy: int; total_rooms: int
    base_price: Money; amenity_codes: frozenset[str]; description: str = ""; is_active: bool = True

@dataclass(slots=True)
class Property:
    id: UUID; owner_account_id: UUID; name: str; property_type: PropertyType
    star_rating: int | None; address: Address; amenity_codes: frozenset[str]
    pricing_strategy: PricingStrategyCode; cancellation_policy: CancellationPolicyCode
    check_in_time: time; check_out_time: time; description: str
    room_types: list[RoomType]; is_active: bool = True
    @classmethod
    def onboard(cls, *, owner_account_id: UUID, draft: PropertyDraft, known_amenities: frozenset[str],
                rules: Sequence[PropertyTypeRule]) -> "Property": ...
    def add_room_type(self, draft: RoomTypeDraft, known_amenities: frozenset[str],
                      rules: Sequence[PropertyTypeRule]) -> RoomType: ...   # re-runs applicable rules after appending
```

Invariants:
- Name 2–200 characters; `star_rating` 1–5 or None → else `ValidationFailed(INVALID_PROPERTY)`.
- At least one room type → else `ValidationFailed(ROOM_TYPE_REQUIRED)`.
- Room type `max_occupancy` 1–10, `total_rooms` 1–500, `base_price` > 0 → else `ValidationFailed(INVALID_PROPERTY)`.
- Room-type names unique per property, case-insensitive → else `Conflict(DUPLICATE_ROOM_TYPE_NAME)`.
- Amenity codes must be known → else `ValidationFailed(UNKNOWN_AMENITIES)`.

```python
# catalog/domain/property_rules.py — extension point for property kinds
class PropertyTypeRule(ABC):
    applies_to: ClassVar[frozenset[PropertyType]]
    @abstractmethod
    def validate(self, property_: Property) -> None: ...   # BusinessRuleViolation(PROPERTY_TYPE_RULE_VIOLATED, reason=...)
class StarRatingRequiredRule(PropertyTypeRule): ...        # HOTEL, RESORT need a star rating
class EntirePlaceRule(PropertyTypeRule): ...               # VILLA: exactly one room type with total_rooms == 1
DEFAULT_PROPERTY_RULES: tuple[PropertyTypeRule, ...] = (StarRatingRequiredRule(), EntirePlaceRule())
```

Commands (`catalog/services/commands.py`):
- `OwnerAccountDraft(display_name, contact_email, contact_phone)`
- `RoomTypeDraft(name, max_occupancy, total_rooms, base_price: Decimal, amenity_codes: frozenset[str], description="")`
- `PropertyDraft(name, property_type, star_rating, address: Address, amenity_codes, pricing_strategy, cancellation_policy, check_in_time, check_out_time, description, room_types: tuple[RoomTypeDraft, ...])`

### 4.3 Interfaces

```python
# catalog/interfaces/repositories.py
class OwnerAccountRepository(ABC):
    def add(self, account: OwnerAccount) -> None: ...          # UNIQUE contact_email_hash -> Conflict(OWNER_ACCOUNT_EXISTS)
    def get(self, account_id: UUID) -> OwnerAccount: ...       # NotFound(OWNER_ACCOUNT_NOT_FOUND)
class PropertyRepository(ABC):
    def add(self, property_: Property) -> None: ...            # property + its room types
    def add_room_type(self, room_type: RoomType) -> None: ...  # unique-name violation -> Conflict(DUPLICATE_ROOM_TYPE_NAME)
    def get(self, property_id: UUID) -> Property: ...          # NotFound(PROPERTY_NOT_FOUND)
    def list_for_account(self, account_id: UUID) -> list[Property]: ...
    def get_room_type(self, room_type_id: UUID) -> tuple[Property, RoomType]: ...   # NotFound(ROOM_TYPE_NOT_FOUND)
class AmenityRepository(ABC):
    def known_codes(self) -> frozenset[str]: ...
    def list_all(self) -> list[Amenity]: ...                   # Amenity(code, name, category) frozen dataclass

# catalog/interfaces/ports.py
class InventoryProvisioningPort(ABC):
    def provision(self, room_type_id: UUID, total_rooms: int) -> None: ...
```

The blind index on the owner's email (`contact_email_hash`, unique) prevents two owner accounts sharing a contact email without ever querying plaintext.

### 4.4 Services (constructor dependencies in brackets)

| Service | Method | Steps |
|---|---|---|
| `OwnerAccountService(accounts, uow)` | `open_account(owner_user_id, draft) -> OwnerAccount` | validate → `OwnerAccount.open` → `accounts.add` |
| `PropertyOnboardingService(accounts, properties, amenities, provisioning, events, uow, rules=DEFAULT_PROPERTY_RULES)` | `onboard(account_id, draft) -> Property` | uow: `accounts.get` → `Property.onboard` → `properties.add` → `provision()` each room type → publish `PropertyOnboarded` |
| `RoomTypeService(properties, amenities, provisioning, events, uow, rules=DEFAULT_PROPERTY_RULES)` | `add_room_type(property_id, draft) -> RoomType` | uow: `properties.get` → `property.add_room_type(...)` → `properties.add_room_type` → `provision()` → publish `RoomTypeAdded` |
| `CatalogQueryService(accounts, properties, amenities)` | `get_property`, `list_properties(account_id)`, `list_amenities`, `room_type_snapshot(room_type_id)`, `owner_user_id(account_id)` | reads (`room_type_snapshot.is_active = property.is_active and room_type.is_active`) |

`catalog/management/commands/seed_demo.py` is idempotent (skips existing accounts by display name). It creates:
- a chain owner (user `owner-chain`) with two properties: a Goa RESORT on WEEKEND pricing with the MODERATE policy, and a Bengaluru HOTEL on DYNAMIC pricing with FLEXIBLE.
- a standalone owner (user `owner-solo`) with one Goa VILLA on STANDARD pricing, NON_REFUNDABLE.

### 4.5 Public package

```python
# catalog/public/dto.py
@dataclass(frozen=True, slots=True)
class RoomTypeSummary:
    room_type_id: UUID; name: str; max_occupancy: int; total_rooms: int
    base_price: Money; amenity_codes: frozenset[str]; is_active: bool

@dataclass(frozen=True, slots=True)
class PropertySnapshot:
    property_id: UUID; owner_account_id: UUID; name: str; property_type: PropertyType
    star_rating: int | None; city: str; locality: str; amenity_codes: frozenset[str]
    pricing_strategy: PricingStrategyCode; is_active: bool; room_types: tuple[RoomTypeSummary, ...]

@dataclass(frozen=True, slots=True)
class RoomTypeSnapshot:
    room_type_id: UUID; property_id: UUID; owner_account_id: UUID; property_name: str; room_type_name: str
    max_occupancy: int; total_rooms: int; base_price: Money
    pricing_strategy: PricingStrategyCode; cancellation_policy: CancellationPolicyCode
    check_in_time: time; is_active: bool

# catalog/public/events.py
@dataclass(frozen=True, kw_only=True)
class PropertyOnboarded(DomainEvent):
    property: PropertySnapshot
@dataclass(frozen=True, kw_only=True)
class RoomTypeAdded(DomainEvent):
    property: PropertySnapshot
    room_type_id: UUID

# catalog/public/api.py
class CatalogAPI:
    def room_type_snapshot(self, room_type_id: UUID) -> RoomTypeSnapshot: ...   # NotFound(ROOM_TYPE_NOT_FOUND)
    def owner_user_id(self, account_id: UUID) -> str | None: ...
def get_catalog_api() -> CatalogAPI: ...
```

### 4.6 Tables

**`catalog.owner_account`** and `catalog.owner_account_log`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | uuid7 |
| display_name | varchar(200) | |
| owner_user_id | varchar(64) | JWT `sub` of the creator; indexed |
| contact_email | text | `EncryptedTextField(pi_type=EMAIL)` |
| contact_email_hash | varchar(64) | `BlindIndexField`; **UNIQUE on the live table** |
| masked_contact_email | varchar(255) | |
| contact_phone | text | `EncryptedTextField(pi_type=PHONE)` |
| masked_contact_phone | varchar(255) | |
| + common fields | | |

**`catalog.property`** and `catalog.property_log`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| owner_account | uuid FK → catalog.owner_account | RESTRICT; indexed |
| name, description | varchar(200), text | |
| property_type | smallint | EnumField |
| star_rating | smallint null | CHECK 1–5 |
| address_line1, locality, city, state, postal_code, country | varchar | |
| city_key | varchar(100) | lower(city); indexed |
| amenity_codes | varchar(40)[] | ArrayField |
| pricing_strategy, cancellation_policy | smallint | EnumField |
| check_in_time, check_out_time | time | default 14:00 / 11:00 |
| + common fields | | |

**`catalog.room_type`** and `catalog.room_type_log`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| property | uuid FK → catalog.property | RESTRICT |
| name, description | varchar(120), text | UNIQUE (property, Lower(name)) on the live table |
| max_occupancy | smallint | CHECK 1–10 |
| total_rooms | smallint | CHECK ≥ 1 |
| base_price_paisa | bigint | CHECK > 0 (₹ × 100) |
| currency | char(3) | 'INR' |
| amenity_codes | varchar(40)[] | |
| + common fields | | |

**`catalog.amenity`** (no log): `code varchar(40) PK`, `name varchar(100)`, `category smallint`, + common fields. Seeded by `0002_seed_amenities` with WIFI, PARKING, POOL, GYM, SPA, RESTAURANT, BREAKFAST, AIR_CONDITIONING, PET_FRIENDLY, AIRPORT_SHUTTLE, TV, MINIBAR, BALCONY, KITCHEN.

### 4.7 IDOR resolvers (`CatalogConfig.ready()`)

- `owner_account` → `Ownership(owner_user_ids={account.owner_user_id})`
- `property` → `Ownership(owner_user_ids={owner of the property's account})`

### 4.8 Status codes (`catalog/status_code.py`)

| Code | Name | HTTP |
|---|---|---|
| 2101001 | OWNER_ACCOUNT_NOT_FOUND | 404 |
| 2101002 | PROPERTY_NOT_FOUND | 404 |
| 2101003 | ROOM_TYPE_NOT_FOUND | 404 |
| 2101004 | UNKNOWN_AMENITIES `"Unknown amenity codes: {codes}"` | 400 |
| 2101005 | DUPLICATE_ROOM_TYPE_NAME `"Room type '{name}' already exists"` | 409 |
| 2101006 | PROPERTY_TYPE_RULE_VIOLATED `"{reason}"` | 422 |
| 2101007 | INVALID_PROPERTY `"{reason}"` | 400 |
| 2101008 | ROOM_TYPE_REQUIRED `"At least one room type is required"` | 400 |
| 2101009 | INVALID_CONTACT `"{reason}"` | 400 |
| 2101010 | OWNER_ACCOUNT_EXISTS `"An owner account with this contact email already exists"` | 409 |

## 5. `inventory` app (module 02)

### 5.1 Enums

```python
class HoldStatus(Enum):     HELD = 1; CONFIRMED = 2; RELEASED = 3
class ReleaseReason(Enum):  CANCELLED = 1; EXPIRED = 2
```

### 5.2 Domain

```python
@dataclass(slots=True)
class Allotment:
    room_type_id: UUID; total_rooms: int

@dataclass(slots=True)
class RoomNight:
    room_type_id: UUID; stay_date: date; total_rooms: int; reserved_rooms: int
    @property
    def available(self) -> int: ...

@dataclass(slots=True)
class InventoryHold:
    id: UUID; room_type_id: UUID; stay: DateRange; rooms: int; reference: str
    status: HoldStatus; expires_at: datetime | None
    release_reason: ReleaseReason | None = None; released_at: datetime | None = None
    @classmethod
    def place(cls, *, room_type_id, stay, rooms, reference, expires_at) -> "InventoryHold": ...
    def confirm(self) -> None: ...
    # HELD -> CONFIRMED; CONFIRMED -> no-op; RELEASED -> Conflict(INVALID_HOLD_TRANSITION, state=...)
    def release(self, reason: ReleaseReason, now: datetime) -> bool: ...
    # HELD/CONFIRMED -> RELEASED and returns True (caller decrements counters); RELEASED -> False (idempotent)

def ensure_bookable(nights: Sequence[RoomNight], stay: DateRange, rooms: int) -> None: ...
# len(nights) != stay.nights or any available < rooms -> Conflict(INVENTORY_UNAVAILABLE)
```

### 5.3 Interfaces

```python
class AllotmentRepository(ABC):
    def upsert(self, allotment: Allotment) -> None: ...
    def get(self, room_type_id: UUID) -> Allotment | None: ...
    def all(self) -> list[Allotment]: ...

class RoomNightRepository(ABC):
    def ensure_rows(self, room_type_id: UUID, total_rooms: int, dates: Sequence[date]) -> None: ...
        # bulk_create(ignore_conflicts=True) — INSERT ... ON CONFLICT DO NOTHING
    def lock_for_stay(self, room_type_id: UUID, stay: DateRange) -> list[RoomNight]: ...
        # SELECT ... WHERE room_type_id=%s AND stay_date >= start AND stay_date < end ORDER BY stay_date FOR UPDATE
    def add_reserved(self, room_type_id: UUID, stay: DateRange, delta: int) -> None: ...
        # UPDATE ... SET reserved_rooms = reserved_rooms + delta, in its own savepoint;
        # IntegrityError (CHECK) -> Conflict(INVENTORY_UNAVAILABLE)
    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange) -> dict[UUID, int]: ...
        # SELECT room_type_id, MIN(total_rooms - reserved_rooms) WHERE stay_date in [start, end)
        # GROUP BY room_type_id HAVING COUNT(*) = stay.nights
    def occupancy(self, room_type_id: UUID, dates: Sequence[date]) -> dict[date, Decimal]: ...

class HoldRepository(ABC):
    def add(self, hold: InventoryHold) -> None: ...
    def get_for_update(self, hold_id: UUID) -> InventoryHold: ...   # NotFound(HOLD_NOT_FOUND)
    def save(self, hold: InventoryHold) -> None: ...
```

### 5.4 Service — `InventoryService(allotments, room_nights, holds, uow, clock, horizon_days=365)`

- **Provisioned dates** are always `[today, today + horizon_days)`. A stay is within the horizon when `stay.start >= today` and `stay.end <= today + horizon_days`.
- `provision(room_type_id, total_rooms)`: upsert the allotment, then `ensure_rows` for the provisioned dates.
- `extend_horizon() -> int`: `ensure_rows` for every allotment over the provisioned dates. Run daily by `manage.py extend_inventory_horizon` and by the scheduler.
- `hold(room_type_id, stay, rooms, reference, expires_at) -> HoldView`:
  - Outside the horizon → `BusinessRuleViolation(STAY_OUTSIDE_HORIZON, days=...)`. No allotment → `Conflict(INVENTORY_UNAVAILABLE)`.
  - In the unit of work: `ensure_rows(room_type_id, allotment.total_rooms, stay.dates())` (idempotent, so no stay fails on a missing row) → `lock_for_stay` → `ensure_bookable` → `add_reserved(+rooms)` → `InventoryHold.place` → `holds.add`.
- `confirm_hold(hold_id)`: in the unit of work, `get_for_update` → `confirm` → `save`.
- `release_hold(hold_id, reason)`: in the unit of work, `get_for_update`; if `release()` returned True → `lock_for_stay` → `add_reserved(-rooms)` → `save`. Idempotent.
- `availability(room_type_ids, stay, rooms) -> dict[UUID, int]`: only room types with minimum availability ≥ `rooms`.
- `occupancy(room_type_id, dates) -> dict[date, Decimal]`.

**Global lock order:** `room_night` rows are always locked ordered by `(room_type_id, stay_date)`, and one transaction never locks the nights of more than one hold.

### 5.5 Public — `inventory/public/api.py`

`InventoryAPI` exposes `provision`, `extend_horizon`, `hold`, `confirm_hold`, `release_hold`, `availability` and `occupancy`. `HoldView(hold_id: UUID, status: HoldStatus, expires_at: datetime | None)` is in `public/dto.py`. Enums are re-exported from `public/enums.py`. Factory: `get_inventory_api()`. Management command: `extend_inventory_horizon`.

### 5.6 Tables

| Table | Columns | Constraints |
|---|---|---|
| `inventory.allotment` (no log) | `room_type_id uuid PK`, `total_rooms integer`, + common fields | CHECK total_rooms ≥ 0 |
| `inventory.room_night` (no log) | `id uuid PK`, `room_type_id uuid`, `stay_date date`, `total_rooms integer`, `reserved_rooms integer default 0`, + common fields | UNIQUE (room_type_id, stay_date); CHECK `reserved_rooms >= 0 AND reserved_rooms <= total_rooms` |
| `inventory.hold` + `inventory.hold_log` | `id uuid PK`, `room_type_id uuid`, `check_in date`, `check_out date`, `rooms smallint`, `reference varchar(64)` (indexed), `hold_status smallint`, `expires_at timestamptz null`, `release_reason smallint null`, `released_at timestamptz null`, + common fields | CHECK check_out > check_in; CHECK rooms ≥ 1 |

### 5.7 Status codes (2102xxx)

| Code | Name | HTTP |
|---|---|---|
| 2102001 | INVENTORY_UNAVAILABLE `"Not enough rooms available for the selected dates"` | 409 |
| 2102002 | HOLD_NOT_FOUND | 404 |
| 2102003 | STAY_OUTSIDE_HORIZON `"Bookings can be made up to {days} days ahead"` | 422 |
| 2102004 | INVALID_HOLD_TRANSITION `"Hold is {state}"` | 409 |

Inventory has no HTTP endpoints; other contexts use `InventoryAPI`.

## 6. `pricing` app (module 03)

```python
# pricing/domain/
@dataclass(frozen=True, slots=True)
class NightlyRate:
    night: date; amount: Money

@dataclass(frozen=True, slots=True)
class PricingRequest:
    room_type_id: UUID; base_price: Money; strategy: PricingStrategyCode; stay: DateRange; rooms: int

@dataclass(frozen=True, slots=True)
class PriceQuote:
    room_type_id: UUID; strategy: PricingStrategyCode; rooms: int; nightly_rates: tuple[NightlyRate, ...]
    @property
    def per_room_total(self) -> Money: ...
    @property
    def total(self) -> Money: ...               # per_room_total × rooms
    @property
    def average_nightly(self) -> Money: ...     # per_room_total / nights

class OccupancyLookup(Protocol):
    def __call__(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]: ...

class PricingStrategy(ABC):
    code: ClassVar[PricingStrategyCode]
    @abstractmethod
    def nightly_rates(self, request: PricingRequest, occupancy: OccupancyLookup) -> tuple[NightlyRate, ...]: ...

class StandardPricing(PricingStrategy): ...   # base price every night
class WeekendPricing(PricingStrategy): ...    # Friday and Saturday nights × 1.20
class DynamicPricing(PricingStrategy): ...    # weekend factor, then occupancy surge ≥ 0.90 × 1.25, ≥ 0.70 × 1.10

class PricingStrategyRegistry:
    def register(self, strategy: PricingStrategy) -> None: ...
    def get(self, code: PricingStrategyCode) -> PricingStrategy: ...   # BusinessRuleViolation(UNSUPPORTED_PRICING_STRATEGY)
    def codes(self) -> frozenset[PricingStrategyCode]: ...

# pricing/interfaces/ports.py
class OccupancyPort(ABC):
    def occupancy(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]: ...

# pricing/services/pricing_service.py
class PricingService:
    def __init__(self, registry: PricingStrategyRegistry, occupancy: OccupancyPort): ...
    def quote(self, request: PricingRequest) -> PriceQuote: ...
    def quote_many(self, requests: Sequence[PricingRequest]) -> dict[UUID, PriceQuote]: ...

# pricing/public/dto.py + api.py
@dataclass(frozen=True, slots=True)
class QuoteDTO:
    room_type_id: UUID; strategy: PricingStrategyCode; rooms: int
    nightly_rates: tuple[tuple[date, Money], ...]; total: Money; average_nightly: Money

class PricingAPI:
    def quote(self, request: PricingRequest) -> QuoteDTO: ...
    def quote_many(self, requests: Sequence[PricingRequest]) -> dict[UUID, QuoteDTO]: ...
def get_pricing_api() -> PricingAPI: ...       # PricingRequest re-exported from pricing/public/dto.py
```

Rules:
- Weekend nights are those where `night.weekday()` is 4 (Fri) or 5 (Sat). Factors multiply; each night is rounded to 0.01 after all factors.
- The occupancy adapter (`pricing/adapters/inventory_occupancy.py`) calls `InventoryAPI.occupancy`; only DYNAMIC calls the lookup.
- **DynamicPricing and its occupancy adapter are built last.** Until then, DYNAMIC is registered as an alias of WEEKEND.

Status code: `2103001 UNSUPPORTED_PRICING_STRATEGY` (422). A unit test asserts every `PricingStrategyCode` is registered.

## 7. `discovery` app (module 04)

### 7.1 Domain

```python
class SortOrder(Enum):
    PRICE_LOW_TO_HIGH = 1; PRICE_HIGH_TO_LOW = 2; RATING_HIGH_TO_LOW = 3   # wire: price_asc | price_desc | rating_desc

@dataclass(frozen=True, slots=True)
class SearchCriteria:
    city: str; stay: DateRange; guests: int; rooms: int = 1; locality: str | None = None
    min_price: Amount | None = None; max_price: Amount | None = None
    amenities: frozenset[str] = frozenset(); min_star_rating: int | None = None
    property_types: frozenset[PropertyType] = frozenset()
    sort: SortOrder = SortOrder.PRICE_LOW_TO_HIGH; page: int = 1; page_size: int = 20
    @property
    def min_occupancy_per_room(self) -> int: ...   # ceil(guests / rooms)

@dataclass(frozen=True, slots=True)
class Listing:                                     # one row per room type
    room_type_id: UUID; property_id: UUID; property_name: str; property_type: PropertyType
    star_rating: int | None; city: str; city_key: str; locality: str; amenity_codes: frozenset[str]
    room_type_name: str; max_occupancy: int; base_price: Money
    pricing_strategy: PricingStrategyCode; is_active: bool

@dataclass(frozen=True, slots=True)
class OfferQuote:                                  # discovery's own price view (anti-corruption)
    strategy: PricingStrategyCode; nightly_rates: tuple[tuple[date, Money], ...]
    total: Money; average_nightly: Money

@dataclass(frozen=True, slots=True)
class RoomOffer:
    listing: Listing; available_rooms: int; quote: OfferQuote

@dataclass(frozen=True, slots=True)
class PropertyResult:
    property_id: UUID; property_name: str; property_type: PropertyType; star_rating: int | None
    city: str; locality: str; amenity_codes: frozenset[str]; offers: tuple[RoomOffer, ...]
    @property
    def lowest_nightly_price(self) -> Money: ...

@dataclass(frozen=True, slots=True)
class SearchPage:
    items: tuple[PropertyResult, ...]; page: int; page_size: int; total: int
```

```python
# discovery/domain/filters.py — storage-agnostic predicates
class Op(Enum):  EQ = 1; IEQ = 2; IN = 3; GTE = 4; LTE = 5; CONTAINS_ALL = 6

@dataclass(frozen=True, slots=True)
class Predicate:
    field: str; op: Op; value: object

class FilterStage(Enum):  INDEX = 1; OFFER = 2

class SearchFilter(ABC):
    name: ClassVar[str]
    stage: ClassVar[FilterStage]
    @abstractmethod
    def is_active(self, criteria: SearchCriteria) -> bool: ...
    def predicate(self, criteria: SearchCriteria) -> Predicate: ...           # INDEX stage
    def matches(self, offer: RoomOffer, criteria: SearchCriteria) -> bool: ... # OFFER stage

class FilterRegistry:
    def register(self, filter_cls: type[SearchFilter]) -> type[SearchFilter]: ...   # usable as a decorator
    def active(self, criteria: SearchCriteria, stage: FilterStage) -> list[SearchFilter]: ...

filter_registry = FilterRegistry()
register_filter = filter_registry.register
```

| Filter | Stage | Rule |
|---|---|---|
| `ActiveListingFilter` | INDEX | `is_active EQ True` (always active) |
| `CityFilter` | INDEX | `city_key EQ criteria.city.strip().lower()` |
| `LocalityFilter` | INDEX | `locality IEQ` |
| `OccupancyFilter` | INDEX | `max_occupancy GTE ceil(guests / rooms)` |
| `StarRatingFilter` | INDEX | `star_rating GTE` |
| `AmenitiesFilter` | INDEX | `amenity_codes CONTAINS_ALL` |
| `PropertyTypeFilter` | INDEX | `property_type IN` |
| `PriceRangeFilter` | OFFER | `min_price ≤ quote.average_nightly ≤ max_price` |

Sorting uses a `ResultSorter` per `SortOrder` in a registry. Price sorts by `lowest_nightly_price`. Rating sorts `star_rating` descending (None last), then by price.

### 7.2 Interfaces and services

```python
class ListingRepository(ABC):
    def upsert_many(self, listings: Sequence[Listing]) -> None: ...
    def find(self, predicates: Sequence[Predicate], limit: int) -> list[Listing]: ...
class AvailabilityPort(ABC):
    def available_rooms(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]: ...
class QuotePort(ABC):
    def quote_many(self, listings: Sequence[Listing], stay: DateRange, rooms: int) -> dict[UUID, OfferQuote]: ...
```

- The Django repository translates predicates to `Q` (`CONTAINS_ALL` → `amenity_codes__contains`, `IEQ` → `__iexact`, `IN` → `__in`); the in-memory repository evaluates them in Python.
- Adapters `discovery/adapters/inventory_availability.py` and `discovery/adapters/pricing_quotes.py` map `QuoteDTO` → `OfferQuote`.
- `SearchService(listings, availability, quotes, filters=filter_registry, sorters=SORTERS, max_candidates=2000).search(criteria) -> SearchPage`:
  1. INDEX predicates → `find` (log a WARNING if the cap is hit).
  2. `available_rooms` → drop unavailable room types.
  3. `quote_many`.
  4. OFFER filters → group by property → sort → paginate.
- `ListingProjector(listings)`: `on_property_onboarded(event)` and `on_room_type_added(event)` map the `PropertySnapshot` to `Listing` rows and call `upsert_many`. They are subscribed to `event_bus` in `DiscoveryConfig.ready()`.

### 7.3 Table — `discovery.listing` (projection, no log)

`room_type_id uuid PK`, `property_id uuid` (indexed), `property_name`, `property_type smallint`, `star_rating smallint null`, `city`, `city_key`, `locality`, `amenity_codes varchar(40)[]` (GIN index), `room_type_name`, `max_occupancy smallint`, `base_price_paisa bigint`, `currency`, `pricing_strategy smallint`, `is_active bool`, + common fields. Composite index on `(city_key, is_active)`.

Status code: `2104001 INVALID_SEARCH "{reason}"` (400).

## 8. `booking` app (module 05)

### 8.1 Enums

```python
class BookingStatus(Enum):        PENDING_PAYMENT = 1; CONFIRMED = 2; CANCELLED = 3; EXPIRED = 4
class BookingType(Enum):          PREPAID = 1; PAY_AT_PROPERTY = 2
class BookingTrigger(Enum):       CONFIRM_PAYMENT = 1; CANCEL = 2; EXPIRE = 3
class BookingRefundStatus(Enum):  NONE = 1; REFUNDED = 2
```

### 8.2 Domain

```python
@dataclass(frozen=True, slots=True)
class GuestContact:                      # PII; name 2-100 chars, email, phone ^[6-9]\d{9}$
    name: str = field(repr=False); email: str = field(repr=False); phone: str = field(repr=False)

@dataclass(frozen=True, slots=True)
class BookingPrice:
    nightly_rates: tuple[tuple[date, Money], ...]; rooms: int; total: Money
    pricing_strategy: PricingStrategyCode

@dataclass(frozen=True, slots=True)
class RoomTypeInfo:                      # booking's own view of catalog data (anti-corruption)
    room_type_id: UUID; property_id: UUID; owner_account_id: UUID; max_occupancy: int
    total_rooms: int; base_price: Money; pricing_strategy: PricingStrategyCode
    cancellation_policy: CancellationPolicyCode; check_in_time: time; is_active: bool

@dataclass(slots=True)
class Booking:
    id: UUID; guest_id: str; property_id: UUID; room_type_id: UUID; owner_account_id: UUID
    stay: DateRange; rooms: int; guests: int; booking_type: BookingType; status: BookingStatus
    price: BookingPrice; cancellation_policy: CancellationPolicyCode; check_in_time: time
    contact: GuestContact; created_at: datetime
    hold_id: UUID | None = None; hold_expires_at: datetime | None = None
    payment_id: UUID | None = None; last_payment_failure: str | None = None
    refund_amount: Amount | None = None; refund_percent: Decimal | None = None
    refund_status: BookingRefundStatus = BookingRefundStatus.NONE; refund_id: UUID | None = None
    cancellation_reason: str | None = None
    confirmed_at: datetime | None = None; cancelled_at: datetime | None = None; expired_at: datetime | None = None

    @staticmethod
    def validate_request(*, room_type: RoomTypeInfo, stay: DateRange, rooms: int, guests: int,
                         today: date, settings: BookingSettings) -> None: ...
    # room type active (ROOM_TYPE_NOT_BOOKABLE); 1 <= nights <= max_nights, stay.start >= today,
    # stay.end <= today + horizon_days (INVALID_BOOKING_REQUEST); 1 <= rooms <= min(10, total_rooms)
    # (INVALID_BOOKING_REQUEST); 1 <= guests <= rooms * max_occupancy (GUEST_CAPACITY_EXCEEDED)
    @classmethod
    def request(cls, *, guest_id: str, room_type: RoomTypeInfo, stay: DateRange, rooms: int, guests: int,
                type_policy: BookingTypePolicy, contact: GuestContact, price: BookingPrice, now: datetime) -> "Booking": ...
    def attach_hold(self, hold_id: UUID, expires_at: datetime | None) -> None: ...
    def hold_expired(self, now: datetime) -> bool: ...   # hold_expires_at is not None and now >= hold_expires_at
    def check_in_at(self) -> datetime: ...               # stay.start at check_in_time, Asia/Kolkata, aware
    def confirm_payment(self, payment_id: UUID, now: datetime) -> None: ...   # trigger CONFIRM_PAYMENT
    def record_payment_failure(self, reason: str) -> None: ...                # status unchanged
    def cancel(self, *, now: datetime, reason: str | None, refund: RefundDecision) -> None: ...
    # trigger CANCEL; sets refund_amount/percent
    def expire(self, now: datetime) -> None: ...                              # trigger EXPIRE
    def record_refund(self, refund_id: UUID) -> None: ...                     # refund_status = REFUNDED
```

`BookingSettings` (`booking/services/settings.py`): `@dataclass(frozen=True) BookingSettings(hold_ttl: timedelta, max_nights: int, horizon_days: int)`. `container.py` builds it from `BOOKING_HOLD_TTL_SECONDS`, `MAX_STAY_NIGHTS` and `INVENTORY_HORIZON_DAYS`.

```python
# booking/domain/state_machine.py
TRANSITIONS: Mapping[tuple[BookingStatus, BookingTrigger], BookingStatus] = {
    (BookingStatus.PENDING_PAYMENT, BookingTrigger.CONFIRM_PAYMENT): BookingStatus.CONFIRMED,
    (BookingStatus.PENDING_PAYMENT, BookingTrigger.CANCEL): BookingStatus.CANCELLED,
    (BookingStatus.PENDING_PAYMENT, BookingTrigger.EXPIRE): BookingStatus.EXPIRED,
    (BookingStatus.CONFIRMED, BookingTrigger.CANCEL): BookingStatus.CANCELLED,
}
class BookingStateMachine:
    @staticmethod
    def next_state(current: BookingStatus, trigger: BookingTrigger) -> BookingStatus: ...
    # missing pair -> Conflict(INVALID_STATE_TRANSITION, action=trigger.name.lower(), state=current.name)
    @staticmethod
    def allowed_triggers(current: BookingStatus) -> frozenset[BookingTrigger]: ...
```

```python
# booking/domain/booking_types.py
class BookingTypePolicy(ABC):
    booking_type: ClassVar[BookingType]
    initial_status: ClassVar[BookingStatus]
    requires_online_payment: ClassVar[bool]
    @abstractmethod
    def hold_expires_at(self, now: datetime, hold_ttl: timedelta) -> datetime | None: ...
class PrepaidBookingPolicy(BookingTypePolicy): ...        # PENDING_PAYMENT, True, now + ttl
class PayAtPropertyBookingPolicy(BookingTypePolicy): ...  # CONFIRMED, False, None (hold confirmed at once)
class BookingTypeRegistry:
    def get(self, booking_type: BookingType) -> BookingTypePolicy: ...
```

```python
# booking/domain/cancellation.py
@dataclass(frozen=True, slots=True)
class RefundDecision:
    amount: Money; percent: Decimal; rule: str
    @classmethod
    def nothing(cls, rule: str = "nothing paid") -> "RefundDecision": ...

@dataclass(frozen=True, slots=True)
class RefundTier:
    min_hours_before_check_in: int; refund_percent: Decimal

class CancellationPolicy(ABC):
    code: CancellationPolicyCode
    @abstractmethod
    def evaluate(self, *, amount_paid: Amount, check_in_at: datetime, cancelled_at: datetime) -> RefundDecision: ...

class TieredCancellationPolicy(CancellationPolicy):
    def __init__(self, code: CancellationPolicyCode, tiers: Sequence[RefundTier]): ...
    # hours_before = (check_in_at - cancelled_at) / 1h; tiers sorted descending; the first tier with
    # hours_before >= min_hours applies; otherwise 0 %

class CancellationPolicyRegistry:
    def register(self, policy: CancellationPolicy) -> None: ...
    def get(self, code: CancellationPolicyCode) -> CancellationPolicy: ...   # BusinessRuleViolation(UNSUPPORTED_CANCELLATION_POLICY)

DEFAULT_CANCELLATION_POLICIES = (
    TieredCancellationPolicy(FLEXIBLE,       [RefundTier(24, Decimal(100)), RefundTier(0, Decimal(50))]),
    TieredCancellationPolicy(MODERATE,       [RefundTier(120, Decimal(100)), RefundTier(24, Decimal(50))]),
    TieredCancellationPolicy(STRICT,         [RefundTier(168, Decimal(50))]),
    TieredCancellationPolicy(NON_REFUNDABLE, []),
)
```

Cancelling at or after `check_in_at()` → `BusinessRuleViolation(CANCELLATION_WINDOW_CLOSED)`.

### 8.3 Interfaces

```python
# booking/interfaces/repositories.py
class BookingRepository(ABC):
    def add(self, booking: Booking) -> None: ...
    def get(self, booking_id: UUID) -> Booking: ...                 # NotFound(BOOKING_NOT_FOUND)
    def get_for_update(self, booking_id: UUID) -> Booking: ...      # SELECT ... FOR UPDATE
    def save(self, booking: Booking) -> None: ...
    def list_for_guest(self, guest_id: str, *, page: int, page_size: int) -> tuple[list[Booking], int]: ...
    def list_for_property(self, property_id: UUID, *, page: int, page_size: int) -> tuple[list[Booking], int]: ...
    def find_expired_pending_ids(self, now: datetime, limit: int) -> list[UUID]: ...
        # plain SELECT: booking_status = PENDING_PAYMENT AND hold_expires_at <= now ORDER BY hold_expires_at
    def lock_if_expired_pending(self, booking_id: UUID, now: datetime) -> Booking | None: ...
        # same predicate + id, FOR UPDATE SKIP LOCKED; None if gone, already handled, or locked elsewhere

# booking/interfaces/ports.py
class CatalogPort(ABC):
    def room_type_info(self, room_type_id: UUID) -> RoomTypeInfo: ...
class PricingPort(ABC):
    def quote(self, room_type: RoomTypeInfo, stay: DateRange, rooms: int) -> BookingPrice: ...
class InventoryPort(ABC):
    def hold(self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str,
             expires_at: datetime | None) -> UUID: ...
    def confirm_hold(self, hold_id: UUID) -> None: ...
    def release_hold(self, hold_id: UUID, reason: Literal["CANCELLED", "EXPIRED"]) -> None: ...

@dataclass(frozen=True, slots=True)
class ChargeRequest:
    booking_id: UUID; payer_id: str; amount: Money; method: str
    method_details: Mapping[str, Any] = field(repr=False); idempotency_key: str = ""
@dataclass(frozen=True, slots=True)
class ChargeResult:
    payment_id: UUID; succeeded: bool; status: str; instrument_display: str
    failure_code: str | None; failure_message: str | None
@dataclass(frozen=True, slots=True)
class RefundRequest:
    payment_id: UUID; amount: Money; reason: str; idempotency_key: str
@dataclass(frozen=True, slots=True)
class RefundResult:
    refund_id: UUID

class PaymentPort(ABC):
    def find_charge(self, idempotency_key: str) -> ChargeResult | None: ...
    def charge(self, request: ChargeRequest) -> ChargeResult: ...
    def refund(self, request: RefundRequest) -> RefundResult: ...   # gateway failure raises UpstreamFailure(REFUND_FAILED)
```

Adapters in `booking/adapters/`: `CatalogAdapter` (→ `CatalogAPI`), `PricingAdapter` (→ `PricingAPI`), `InventoryAdapter` (→ `InventoryAPI`), `PaymentAdapter` (→ `PaymentAPI`). Each maps the other context's DTOs to booking's port DTOs; booking never imports another app's enums except catalog's published `PricingStrategyCode` and `CancellationPolicyCode`.

### 8.4 Services

Commands (`booking/services/commands.py`):
- `CreateBookingCommand(guest_id, room_type_id, check_in, check_out, rooms, guests, booking_type, contact: GuestContact = field(repr=False))`
- `PayBookingCommand(booking_id, payer_id, method, method_details = field(repr=False), idempotency_key)`
- `CancelBookingCommand(booking_id, actor_id, reason)`

**`CreateBookingService(bookings, catalog, pricing, inventory, booking_types, events, uow, clock, settings).execute(cmd) -> Booking`**
1. `stay = DateRange(cmd.check_in, cmd.check_out)`; `info = catalog.room_type_info(cmd.room_type_id)`; `type_policy = booking_types.get(cmd.booking_type)`.
2. `Booking.validate_request(...)` — before any pricing work.
3. `price = pricing.quote(info, stay, cmd.rooms)`; `booking = Booking.request(...)`.
4. In the unit of work: `hold_id = inventory.hold(..., reference=str(booking.id), expires_at=type_policy.hold_expires_at(now, settings.hold_ttl))` → `booking.attach_hold(...)` → for PAY_AT_PROPERTY `inventory.confirm_hold(hold_id)` → `bookings.add(booking)` → publish `BookingCreated`.

**`PayBookingService(bookings, payments, inventory, events, uow, clock).execute(cmd) -> PayBookingResult`**

`PayBookingResult(booking: Booking, outcome: Literal["PAID", "DECLINED", "EXPIRED"], payment: ChargeResult | None)`

```text
key = f"pay:{cmd.booking_id}:{cmd.idempotency_key}"
with uow:
    booking = bookings.get_for_update(cmd.booking_id)
    prior = payments.find_charge(key)                         # durable replay, works without Redis
    if prior: return PayBookingResult(booking, "PAID" if prior.succeeded else "DECLINED", prior)
    if booking.booking_type is PAY_AT_PROPERTY: raise BusinessRuleViolation(BOOKING_NOT_PAYABLE)
    if booking.status is EXPIRED: return PayBookingResult(booking, "EXPIRED", None)
    if booking.status is PENDING_PAYMENT and booking.hold_expired(now):
        booking.expire(now); inventory.release_hold(booking.hold_id, "EXPIRED"); bookings.save(booking)
        publish BookingExpired; return PayBookingResult(booking, "EXPIRED", None)        # committed
    if booking.status is not PENDING_PAYMENT:
        raise Conflict(INVALID_STATE_TRANSITION, action="pay", state=booking.status.name)
    result = payments.charge(ChargeRequest(booking.id, cmd.payer_id, booking.price.total, cmd.method,
                                           cmd.method_details, key))
    if result.succeeded:
        booking.confirm_payment(result.payment_id, now); inventory.confirm_hold(booking.hold_id)
        bookings.save(booking); publish BookingConfirmed; return PayBookingResult(booking, "PAID", result)
    booking.record_payment_failure(result.failure_code); bookings.save(booking)
    return PayBookingResult(booking, "DECLINED", result)                                  # committed
```

The pay view never raises for an outcome:
- PAID → `api_response(data, http_status=200)`
- DECLINED → `api_response(data, http_status=402, status=PAYMENT_DECLINED, message=PAYMENT_DECLINED.render(reason=...))`
- EXPIRED → `api_response(data, http_status=409, status=BOOKING_EXPIRED)`

`data = {"booking": ..., "payment": ... | null}`.

**`CancelBookingService(bookings, inventory, payments, policies, events, uow, clock).execute(cmd) -> Booking`**

```text
with uow:
    booking = bookings.get_for_update(cmd.booking_id)
    if now >= booking.check_in_at(): raise BusinessRuleViolation(CANCELLATION_WINDOW_CLOSED)
    decision = (policies.get(booking.cancellation_policy).evaluate(amount_paid=booking.price.total,
                    check_in_at=booking.check_in_at(), cancelled_at=now)
                if booking.status is CONFIRMED and booking.payment_id else RefundDecision.nothing())
    booking.cancel(now=now, reason=cmd.reason, refund=decision)      # state machine guards the transition
    inventory.release_hold(booking.hold_id, "CANCELLED")
    if decision.amount.is_positive():
        refund = payments.refund(RefundRequest(booking.payment_id, decision.amount, "booking cancelled",
                                               idempotency_key=f"refund:{booking.id}"))
        booking.record_refund(refund.refund_id)
    bookings.save(booking); publish BookingCancelled
```

- A refund gateway failure raises `UpstreamFailure(REFUND_FAILED)` (502). The whole cancel rolls back: the booking stays CONFIRMED, the hold stays, and no refund row is written. The client simply retries.
- A PAY_AT_PROPERTY booking is CONFIRMED with no `payment_id`, so it cancels with no refund.

**`ExpireBookingsService(bookings, inventory, events, uow, clock).execute(batch_size=100) -> int`**
- `ids = bookings.find_expired_pending_ids(now, batch_size)`, outside any transaction.
- For each id, in its **own** unit of work: `booking = bookings.lock_if_expired_pending(id, now)` (skip if None) → `expire` → `release_hold(..., "EXPIRED")` → `save` → publish `BookingExpired`.
- Returns the count. Command: `python manage.py expire_pending_bookings [--batch-size N]`.

**`BookingQueryService(bookings)`**: `get(booking_id)`, `list_for_guest(...)`, `list_for_property(...)`.

### 8.5 Public package

`booking/public/events.py`: `BookingCreated`, `BookingConfirmed`, `BookingCancelled`, `BookingExpired`. Each carries `booking_id`, `guest_id` and `property_id`. They have no subscribers today; they are the hook for notifications. No other app calls booking, so there is no `BookingAPI`.

### 8.6 IDOR resolver (`BookingConfig.ready()`)

`booking` → `Ownership(guest_user_ids={booking.guest_id}, owner_user_ids={CatalogAPI.owner_user_id(booking.owner_account_id)})`.

### 8.7 Table — `booking.booking` and `booking.booking_log`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| guest_id | varchar(64) | index (guest_id, creation_date) |
| property_id, room_type_id, owner_account_id | uuid | no FKs (other contexts); property_id indexed |
| check_in, check_out | date | CHECK check_out > check_in |
| rooms, guests | smallint | CHECK ≥ 1 |
| booking_type, booking_status | smallint | EnumField |
| total_amount_paisa | bigint | ₹ × 100 |
| currency | char(3) | |
| nightly_rates | jsonb | `JSONField(encoder=DjangoJSONEncoder)`: `[{"date": "2026-11-20", "amount_paisa": 480000}]` |
| pricing_strategy, cancellation_policy | smallint | snapshots |
| check_in_time | time | snapshot |
| guest_name / masked_guest_name | text / varchar(255) | `EncryptedTextField(pi_type=NAME)` |
| guest_email / masked_guest_email | text / varchar(255) | `EncryptedTextField(pi_type=EMAIL)` |
| guest_phone / masked_guest_phone | text / varchar(255) | `EncryptedTextField(pi_type=PHONE)` |
| hold_id | uuid null | |
| hold_expires_at | timestamptz null | partial index `WHERE booking_status = 1` |
| payment_id, refund_id | uuid null | |
| last_payment_failure | varchar(255) null | |
| refund_amount_paisa | bigint null | ₹ × 100 |
| refund_percent | numeric(5,2) null | |
| refund_status | smallint | |
| cancellation_reason | varchar(255) null | |
| confirmed_at, cancelled_at, expired_at | timestamptz null | |
| + common fields | | `creation_date` is the booking's `created_at` |

### 8.8 Status codes (2105xxx)

| Code | Name | HTTP |
|---|---|---|
| 2105001 | BOOKING_NOT_FOUND | 404 |
| 2105002 | INVALID_STATE_TRANSITION `"Cannot {action} a booking that is {state}"` | 409 |
| 2105003 | BOOKING_EXPIRED `"Booking hold expired; please book again"` | 409 |
| 2105004 | PAYMENT_DECLINED `"Payment declined: {reason}"` | 402 |
| 2105005 | INVALID_BOOKING_REQUEST `"{reason}"` | 400 |
| 2105006 | ROOM_TYPE_NOT_BOOKABLE | 422 |
| 2105007 | CANCELLATION_WINDOW_CLOSED `"Bookings cannot be cancelled after check-in"` | 422 |
| 2105008 | GUEST_CAPACITY_EXCEEDED `"{guests} guests exceed capacity of {capacity}"` | 422 |
| 2105009 | BOOKING_NOT_PAYABLE `"Pay-at-property bookings are paid at the property"` | 422 |
| 2105010 | UNSUPPORTED_CANCELLATION_POLICY | 422 |
| 2105011 | LIST_FILTER_REQUIRED `"property_id is required for owners"` | 400 |

## 9. `payment` app (module 06)

### 9.1 Enums

```python
class PaymentMethod(Enum):   CARD = 1; UPI = 2; WALLET = 3
class PaymentStatus(Enum):   INITIATED = 1; SUCCEEDED = 2; FAILED = 3; PARTIALLY_REFUNDED = 4; REFUNDED = 5
class RefundStatus(Enum):    SUCCEEDED = 1
class WalletProvider(Enum):  PAYTM = 1; PHONEPE = 2; AMAZON_PAY = 3
class CardBrand(Enum):       VISA = 1; MASTERCARD = 2; RUPAY = 3; AMEX = 4
```

### 9.2 Domain

```python
@dataclass(frozen=True, slots=True)
class PaymentInstrument:
    method: PaymentMethod
    display: str                                    # "VISA •••• 4242" | "r****@okaxis" | "PAYTM ******3210"
    sensitive_ref: str | None = field(default=None, repr=False)   # UPI VPA / wallet phone; never card data

@dataclass(frozen=True, slots=True)
class ParsedInstrument:
    instrument: PaymentInstrument
    gateway_payload: Mapping[str, str] = field(repr=False)        # transient; never persisted or logged

class PaymentMethodHandler(ABC):
    method: ClassVar[PaymentMethod]
    @abstractmethod
    def parse(self, details: Mapping[str, Any]) -> ParsedInstrument: ...
    # ValidationFailed(INVALID_PAYMENT_DETAILS, reason=<field name, never the value>)

class CardHandler(PaymentMethodHandler): ...
    # card_number 12-19 digits + Luhn; brand by prefix (4 VISA; 51-55, 2221-2720 MASTERCARD; 60, 65, 81, 82 RUPAY;
    # 34, 37 AMEX); expiry month/year not past; cvv 3 digits (4 for AMEX); holder_name 2-100
class UpiHandler(PaymentMethodHandler): ...      # vpa ^[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}$
class WalletHandler(PaymentMethodHandler): ...   # provider (WalletProvider name), phone ^[6-9]\d{9}$

class PaymentMethodRegistry:
    def register(self, handler: PaymentMethodHandler) -> None: ...
    def get(self, method: PaymentMethod | str) -> PaymentMethodHandler: ...   # ValidationFailed(UNSUPPORTED_PAYMENT_METHOD)

@dataclass(slots=True)
class Payment:
    id: UUID; booking_id: UUID; payer_id: str; amount: Money; instrument: PaymentInstrument
    status: PaymentStatus; idempotency_key: str; refunded_amount: Money
    gateway_txn_id: str | None = None; failure_code: str | None = None; failure_message: str | None = None
    @classmethod
    def initiate(cls, *, booking_id, payer_id, amount, instrument, idempotency_key) -> "Payment": ...
    def mark_succeeded(self, gateway_txn_id: str) -> None: ...
    def mark_failed(self, code: str, message: str) -> None: ...
    def refundable_amount(self) -> Money: ...
    def apply_refund(self, amount: Money) -> None: ...
    # from SUCCEEDED / PARTIALLY_REFUNDED only (BusinessRuleViolation PAYMENT_NOT_REFUNDABLE);
    # amount <= refundable (REFUND_EXCEEDS_PAYMENT); -> REFUNDED when fully refunded, else PARTIALLY_REFUNDED

PAYMENT_TRANSITIONS = {
    PaymentStatus.INITIATED: {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED},
    PaymentStatus.SUCCEEDED: {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED},
    PaymentStatus.PARTIALLY_REFUNDED: {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED},
}   # anything else -> Conflict(INVALID_PAYMENT_TRANSITION)

@dataclass(slots=True)
class Refund:
    id: UUID; payment_id: UUID; amount: Money; status: RefundStatus; reason: str
    idempotency_key: str; gateway_refund_id: str | None = None

class DuplicatePayment(Exception): ...           # payment/domain/errors.py — internal, never reaches HTTP
```

### 9.3 Interfaces

```python
class PaymentRepository(ABC):
    def add(self, payment: Payment) -> None: ...      # unique idempotency_key (own savepoint) -> raises DuplicatePayment
    def save(self, payment: Payment) -> None: ...
    def get(self, payment_id: UUID) -> Payment: ...   # NotFound(PAYMENT_NOT_FOUND)
    def get_for_update(self, payment_id: UUID) -> Payment: ...
    def find_by_idempotency_key(self, key: str) -> Payment | None: ...
class RefundRepository(ABC):
    def add(self, refund: Refund) -> None: ...
    def find_by_idempotency_key(self, key: str) -> Refund | None: ...

@dataclass(frozen=True, slots=True)
class GatewayChargeRequest:
    idempotency_key: str; amount: Money; method: PaymentMethod
    payload: Mapping[str, str] = field(repr=False); reference: str = ""
@dataclass(frozen=True, slots=True)
class GatewayChargeResponse:
    success: bool; transaction_id: str | None; failure_code: str | None; failure_message: str | None
@dataclass(frozen=True, slots=True)
class GatewayRefundRequest:
    idempotency_key: str; transaction_id: str; amount: Money
@dataclass(frozen=True, slots=True)
class GatewayRefundResponse:
    success: bool; refund_id: str | None; failure_code: str | None

class PaymentGateway(ABC):
    def charge(self, request: GatewayChargeRequest) -> GatewayChargeResponse: ...
    def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse: ...
```

### 9.4 Mock gateway — `payment/adapters/mock_gateway.py`

Deterministic and idempotent: the same `idempotency_key` returns the same response (an in-process dict guarded by a lock).

| Input | Result |
|---|---|
| Card number ending `0002` | declined: `card_declined` |
| Card number ending `0069` | declined: `expired_card` |
| UPI VPA starting with `fail` | failed: `upi_collect_rejected` |
| Wallet charge above ₹50,000 | failed: `insufficient_wallet_balance` |
| Anything else | success, `transaction_id = "MOCKTXN-" + uuid4().hex[:12]` |
| Refund of a transaction id containing `REFUNDFAIL` (test hook only) | failure `refund_rejected`; every other refund succeeds with `"MOCKRFD-…"` |

### 9.5 Services

```text
ChargeService(payments, methods, gateway, uow).charge(cmd: ChargeCommand) -> Payment
    existing = payments.find_by_idempotency_key(cmd.idempotency_key); if existing: return existing
    parsed = methods.get(cmd.method).parse(cmd.method_details)
    try:
        with uow:
            payment = Payment.initiate(...); payments.add(payment)       # insert first: a duplicate fails here
            response = gateway.charge(GatewayChargeRequest(cmd.idempotency_key, cmd.amount, method,
                                                           parsed.gateway_payload, str(cmd.booking_id)))
            payment.mark_succeeded(...) if response.success else payment.mark_failed(...); payments.save(payment)
    except DuplicatePayment:
        return payments.find_by_idempotency_key(cmd.idempotency_key)    # outside the failed block
    return payment

RefundService(payments, refunds, gateway, uow).refund(cmd: RefundCommand) -> Refund
    existing = refunds.find_by_idempotency_key(cmd.idempotency_key); if existing: return existing
    with uow:
        payment = payments.get_for_update(cmd.payment_id); payment.apply_refund(cmd.amount)   # validates first
        response = gateway.refund(GatewayRefundRequest(cmd.idempotency_key, payment.gateway_txn_id, cmd.amount))
        if not response.success: raise UpstreamFailure(REFUND_FAILED)                       # rolls back everything
        refund = Refund(..., status=SUCCEEDED, gateway_refund_id=response.refund_id)
        refunds.add(refund); payments.save(payment)
    return refund

PaymentQueryService(payments).get(payment_id) -> Payment
```

### 9.6 Public — `payment/public/`

```python
# dto.py
@dataclass(frozen=True, slots=True)
class ChargeCommand:
    booking_id: UUID; payer_id: str; amount: Money; method: str
    method_details: Mapping[str, Any] = field(repr=False); idempotency_key: str = ""
@dataclass(frozen=True, slots=True)
class RefundCommand:
    payment_id: UUID; amount: Money; reason: str; idempotency_key: str
@dataclass(frozen=True, slots=True)
class PaymentDTO:
    payment_id: UUID; booking_id: UUID; payer_id: str; amount: Money; refunded_amount: Money
    status: PaymentStatus; method: PaymentMethod; instrument_display: str
    gateway_txn_id: str | None; failure_code: str | None; failure_message: str | None
@dataclass(frozen=True, slots=True)
class RefundResultDTO:
    refund_id: UUID; amount: Money

# api.py
class PaymentAPI:
    def find_by_idempotency_key(self, key: str) -> PaymentDTO | None: ...
    def charge(self, command: ChargeCommand) -> PaymentDTO: ...
    def refund(self, command: RefundCommand) -> RefundResultDTO: ...   # UpstreamFailure(REFUND_FAILED)
    def get_payment(self, payment_id: UUID) -> PaymentDTO: ...
def get_payment_api() -> PaymentAPI: ...
```

### 9.7 Tables

**`payment.payment`** and `payment.payment_log`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| booking_id | uuid | indexed; no FK |
| payer_id | varchar(64) | |
| amount_paisa, refunded_amount_paisa | bigint | ₹ × 100 |
| currency | char(3) | |
| method, payment_status | smallint | EnumField |
| instrument_display | varchar(64) | masked |
| instrument_ref | text null | `EncryptedTextField(pi_type=OTHER)` — UPI VPA / wallet phone |
| gateway_txn_id | varchar(64) null | |
| failure_code, failure_message | varchar null | |
| idempotency_key | varchar(200) | UNIQUE on the live table |
| + common fields | | |

**`payment.refund`** and `payment.refund_log`: `id`, `payment` FK → payment.payment (RESTRICT), `amount_paisa bigint`, `currency`, `refund_status`, `reason`, `gateway_refund_id`, `idempotency_key` (UNIQUE on the live table), + common fields.

### 9.8 IDOR and field policy

- `payment` → `Ownership(guest_user_ids={payment.payer_id})`, registered in `PaymentConfig.ready()`.
- `PaymentSerializer.gateway_txn_id = HotelCharField(read_roles=[Role.ADMIN])` — dropped from the response for everyone else.

### 9.9 Status codes (2106xxx)

| Code | Name | HTTP |
|---|---|---|
| 2106001 | PAYMENT_NOT_FOUND | 404 |
| 2106002 | INVALID_PAYMENT_DETAILS `"Invalid {reason}"` | 400 |
| 2106003 | UNSUPPORTED_PAYMENT_METHOD | 400 |
| 2106004 | REFUND_EXCEEDS_PAYMENT | 422 |
| 2106005 | PAYMENT_NOT_REFUNDABLE | 422 |
| 2106006 | INVALID_PAYMENT_TRANSITION | 409 |
| 2106007 | REFUND_FAILED `"Refund could not be processed, please retry"` | 502 |

## 10. HTTP API

### 10.1 Route mounting (`hotel_booking_service/urls.py` — written once in the scaffold step)

```python
API = f"hotel/{settings.API_VERSION}/"
urlpatterns = [
    path(API, include("hotel_commons.api.urls")),     # health, security/public-key, dev/tokens (local), schema, docs
    path(API + "catalog/", include("catalog.urls")),
    path(API + "discovery/", include("discovery.urls")),
    path(API, include("booking.urls")),                # bookings, bookings/<uuid:booking_id>[/payments|/cancel]
    path(API, include("payment.urls")),                # payments/<uuid:payment_id>
]
handler404 = "hotel_commons.api.error_views.not_found"
handler500 = "hotel_commons.api.error_views.server_error"
```

App `urls.py` files use explicit `path()` entries with `ViewSet.as_view({"get": "...", "post": "..."})` and no trailing slashes.

### 10.2 Endpoint contract

Notation: IDOR `resource/key/source/relations`. All bodies are JSON. Responses use the envelope.

| # | Method & path (prefix `/hotel/v1`) | View · action | Roles | IDOR | Idempotency | Throttle | Request | Response `data` |
|---|---|---|---|---|---|---|---|---|
| 1 | `GET /health` | `HealthView.get` | public | — | — | none | — | `{database, redis}` |
| 2 | `GET /security/public-key` | `PublicKeyView.get` | public | — | — | default | — | `{algorithm, public_key_pem}` |
| 3 | `POST /dev/tokens` (local) | `DevTokenView.post` | public | — | — | default | `DevTokenRequestSerializer` | `{token, expires_in}` |
| 4 | `GET /catalog/amenities` | `AmenityViewSet.list` | public | — | — | default | — | `[AmenitySerializer]` |
| 5 | `POST /catalog/owner-accounts` | `OwnerAccountViewSet.create` | OWNER, ADMIN | — | `catalog_write` opt | catalog_write | `OwnerAccountCreateSerializer` | `OwnerAccountSerializer` |
| 6 | `GET /catalog/owner-accounts/{account_id}/properties` | `PropertyViewSet.list_for_account` | OWNER, ADMIN | owner_account/account_id/path/{owner} | — | default | — | `[PropertySerializer]` |
| 7 | `POST /catalog/owner-accounts/{account_id}/properties` | `PropertyViewSet.onboard` | OWNER, ADMIN | same as 6 | `catalog_write` opt | catalog_write | `PropertyOnboardSerializer` | `PropertySerializer` (201) |
| 8 | `GET /catalog/properties/{property_id}` | `PropertyViewSet.retrieve` | public | — | — | default | — | `PropertySerializer` |
| 9 | `POST /catalog/properties/{property_id}/room-types` | `RoomTypeViewSet.create` | OWNER, ADMIN | property/property_id/path/{owner} | `catalog_write` opt | catalog_write | `RoomTypeCreateSerializer` | `RoomTypeSerializer` (201) |
| 10 | `GET /discovery/search` | `SearchViewSet.search` | public | — | — | search | `SearchQuerySerializer` (query) | `SearchPageSerializer` |
| 11 | `POST /bookings` | `BookingViewSet.create` | GUEST, ADMIN | — | `booking_create` opt | booking_write | `BookingCreateSerializer` | `BookingSerializer` (201) |
| 12 | `GET /bookings` | `BookingViewSet.list` | GUEST, OWNER, ADMIN | property/property_id/query/{owner} | — | default | `BookingListQuerySerializer` | paged `BookingSerializer` |
| 13 | `GET /bookings/{booking_id}` | `BookingViewSet.retrieve` | GUEST, OWNER, ADMIN | booking/booking_id/path/{guest, owner} | — | default | — | `BookingSerializer` |
| 14 | `POST /bookings/{booking_id}/payments` | `BookingViewSet.pay` | GUEST, ADMIN | booking/booking_id/path/{guest} | `booking_pay` **required** | payment | `BookingPaymentSerializer` | `{booking, payment}`; 200 / 402 / 409 |
| 15 | `POST /bookings/{booking_id}/cancel` | `BookingViewSet.cancel` | GUEST, ADMIN | booking/booking_id/path/{guest} | `booking_cancel` opt | booking_write | `BookingCancelSerializer` | `BookingSerializer` |
| 16 | `GET /payments/{payment_id}` | `PaymentViewSet.retrieve` | GUEST, ADMIN | payment/payment_id/path/{guest} | — | default | — | `PaymentSerializer` |
| 17 | `GET /schema`, `GET /docs` | drf-spectacular | public | — | — | none | — | OpenAPI 3 / Swagger UI |

`GET /bookings` scoping:
- **GUEST** sees own bookings only (an optional `property_id` narrows them).
- **OWNER** must pass `property_id` (400 `LIST_FILTER_REQUIRED` otherwise); the IDOR rule then checks the property is theirs.
- **ADMIN** passes `property_id` or `guest_id`.

### 10.3 Serializers (fields; `→` marks output-only)

All serializers extend `BaseHotelSerializer` and use `Hotel*Field`s (§3.14). Amounts are `HotelAmountField` (rupee strings on the wire, paisa inside) with a sibling `currency`. Enums are `HotelEnumField` (names on the wire, SMALLINT in the DB).

| Serializer | Fields |
|---|---|
| `DevTokenRequestSerializer` | `user_id` str ^[A-Za-z0-9_.@-]{1,64}$, `roles` list[HotelEnumField(Role)] (min 1) |
| `AmenitySerializer` | → `code`, `name`, `category` |
| `OwnerAccountCreateSerializer` | `display_name` str 2–200, `contact_email` email, `contact_phone` ^[6-9]\d{9}$ |
| `OwnerAccountSerializer` | → `account_id`, `display_name`, `owner_user_id`, `contact_email`, `contact_phone`, `created_at`. Contact fields are `HotelEncryptedCharField(read_roles=[ADMIN], subject_attr="owner_user_id", mask_as=EMAIL/PHONE)` |
| `AddressSerializer` | `line1`, `locality`, `city`, `state`, `postal_code` (^\d{6}$), `country` (default "IN") |
| `RoomTypeCreateSerializer` | `name` 2–120, `description` opt, `max_occupancy` 1–10, `total_rooms` 1–500, `base_price` HotelAmountField(min=1) — rupees in, paisa out, `amenities` list[str] (default []) |
| `PropertyOnboardSerializer` | `name`, `description` opt, `property_type` HotelEnumField, `star_rating` 1–5 opt, `address` AddressSerializer, `amenities` list[str], `pricing_strategy` HotelEnumField (default STANDARD), `cancellation_policy` HotelEnumField (default MODERATE), `check_in_time` (default 14:00), `check_out_time` (default 11:00), `room_types` list[RoomTypeCreateSerializer] (min 1) |
| `RoomTypeSerializer` | → `room_type_id`, `name`, `description`, `max_occupancy`, `total_rooms`, `base_price` Amount, `amenities`, `is_active` |
| `PropertySerializer` | → `property_id`, `owner_account_id`, `name`, `description`, `property_type`, `star_rating`, `address`, `amenities`, `pricing_strategy`, `cancellation_policy`, `check_in_time`, `check_out_time`, `is_active`, `room_types` |
| `SearchQuerySerializer` | `city` req, `check_in` date req, `check_out` date req (> check_in, ≤ MAX_STAY_NIGHTS), `guests` 1–40 req, `rooms` 1–10 (1), `locality`, `min_price`/`max_price` HotelAmountField(min=0) (min ≤ max), `amenities` comma list, `min_rating` 1–5, `property_types` comma EnumNames, `sort` price_asc\|price_desc\|rating_desc, `page` ≥ 1, `page_size` 1–50 (20) |
| `SearchPageSerializer` | → `items[]{property_id, name, property_type, star_rating, city, locality, amenities, lowest_nightly_price Amount, offers[]{room_type_id, room_type, max_occupancy, available_rooms, pricing_strategy, nightly_rates[]{date, price Amount}, total Amount, average_nightly Amount}}`, `page`, `page_size`, `total` |
| `BookingCreateSerializer` | `room_type_id` uuid, `check_in`, `check_out` (> check_in, ≤ MAX_STAY_NIGHTS), `rooms` 1–10 (1), `guests` ≥ 1, `booking_type` HotelEnumField (PREPAID), `guest_name` 2–100, `guest_email` email, `guest_phone` ^[6-9]\d{9}$, `on_behalf_of_user_id` opt. `on_behalf_of_user_id` is `HotelCharField(restrict_roles=[ADMIN])` (403 for anyone else) |
| `BookingListQuerySerializer` | `property_id` uuid opt, `guest_id` opt (ADMIN), `page`, `page_size` |
| `BookingSerializer` | → `booking_id`, `status`, `booking_type`, `property_id`, `room_type_id`, `check_in`, `check_out`, `nights`, `rooms`, `guests`, `total` Amount, `nightly_rates[]{date, price}`, `pricing_strategy`, `cancellation_policy`, `hold_expires_at`, `payment_id`, `refund{amount Amount, percent, status}`, `guest_name`, `guest_email`, `guest_phone`, `last_payment_failure`, `created_at`, `confirmed_at`, `cancelled_at`, `expired_at`. The three guest fields are `HotelEncryptedCharField(read_roles=[ADMIN], subject_attr="guest_id", mask_as=NAME/EMAIL/PHONE)` |
| `BookingPaymentSerializer` | `method` HotelEnumField; exactly one of `card{card_number (write_only), expiry_month 1–12, expiry_year, cvv (write_only), holder_name}`, `upi{vpa}`, `wallet{provider HotelEnumField, phone}`, matching `method` |
| `PaymentSummarySerializer` | → `payment_id`, `status`, `instrument`, `failure_code`, `failure_message` |
| `BookingCancelSerializer` | `reason` str ≤ 255 opt |
| `PaymentSerializer` | → `payment_id`, `booking_id`, `amount` Amount, `refunded_amount` Amount, `status`, `method`, `instrument`, `failure_code`, `failure_message`, `gateway_txn_id` (ADMIN only) |

### 10.4 Examples

Search:

```http
GET /hotel/v1/discovery/search?city=Goa&check_in=2026-11-20&check_out=2026-11-22&guests=2&amenities=WIFI,POOL&min_price=2000&max_price=9000&sort=price_asc
```

```json
{"status": 2100000, "message": "Success", "request_id": "7f9c…", "data": {
  "items": [{"property_id": "0192f5c4-…", "name": "Sea Breeze Resort", "property_type": "RESORT", "star_rating": 4,
             "city": "Goa", "locality": "Candolim", "amenities": ["BREAKFAST", "POOL", "WIFI"],
             "lowest_nightly_price": "4800.00",
             "offers": [{"room_type_id": "0192f5c4-…", "room_type": "Deluxe Sea View", "max_occupancy": 2,
                         "available_rooms": 7, "pricing_strategy": "WEEKEND",
                         "nightly_rates": [{"date": "2026-11-20", "price": "4800.00"},
                                           {"date": "2026-11-21", "price": "4800.00"}],
                         "total": "9600.00",
                         "average_nightly": "4800.00"}]}],
  "page": 1, "page_size": 20, "total": 1}}
```

Create booking:

```http
POST /hotel/v1/bookings
Authorization: Bearer <guest token>
Idempotency-Key: booking-6f1d2c7e-001

{"room_type_id": "0192f5c4-…", "check_in": "2026-11-20", "check_out": "2026-11-22", "rooms": 1, "guests": 2,
 "booking_type": "PREPAID", "guest_name": "Rahul Sharma", "guest_email": "rahul@example.com", "guest_phone": "9876543210"}
```

Response `201`:
- `data.status = "PENDING_PAYMENT"`, `data.total = "9600.00"`, `data.hold_expires_at = "…"`.
- The guest sees their own contact in full; an OWNER sees `"guest_email": "r****@example.com"`.

Pay:

```http
POST /hotel/v1/bookings/0192f5c5-…/payments
Authorization: Bearer <guest token>
Idempotency-Key: pay-0192f5c5-attempt-1

{"method": "CARD", "card": {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2028,
 "cvv": "123", "holder_name": "Rahul Sharma"}}
```

| Outcome | HTTP | `status` | `data` |
|---|---|---|---|
| Paid | 200 | 2100000 | `{"booking": {"status": "CONFIRMED", …}, "payment": {"status": "SUCCEEDED", "instrument": "VISA •••• 4242", …}}` |
| Declined (card ending 0002) | 402 | 2105004 | `{"booking": {"status": "PENDING_PAYMENT", …}, "payment": {"status": "FAILED", "failure_code": "card_declined", …}}` |
| Hold expired | 409 | 2105003 | `{"booking": {"status": "EXPIRED", …}, "payment": null}` |
| Same key again | as first | as first | as first, plus header `Idempotent-Replayed: true` |

Other methods: `{"method": "UPI", "upi": {"vpa": "rahul@okaxis"}}` · `{"method": "WALLET", "wallet": {"provider": "PAYTM", "phone": "9876543210"}}`.

Cancel: `POST /hotel/v1/bookings/{id}/cancel` with `{"reason": "plans changed"}` → `200`, `data.status = "CANCELLED"`, `data.refund = {"amount": "9600.00", "percent": "100.00", "status": "REFUNDED"}`.

Error body: `{"status": 2105002, "message": "Cannot pay a booking that is CANCELLED", "errors": null, "data": null, "request_id": "…"}`.

## 11. Redis keys

| Key | Value | TTL | Written by |
|---|---|---|---|
| `hb:rl:v1:{scope}:{u:<user>\|ip:<ip>}` | ZSET of microsecond timestamps | window | `RedisSlidingWindowRateLimiter` |
| `hb:idem:v1:{scope}:{user}:{key}` | `ENC_…` ciphertext of `{"state", "fingerprint", "http_status", "body"}` | 60 s in progress / 24 h done | `RedisIdempotencyStore` |
| `hb:enc:v1:{sha256(X-Encrypted-Key)}` | `1` | 300 s | `ApiEncryptionMiddleware` (replay guard) |

## 12. Database summary

| Context | Live tables | Log tables | Key constraints |
|---|---|---|---|
| catalog | owner_account, property, room_type, amenity | owner_account_log, property_log, room_type_log | unique contact_email_hash; unique room-type name per property; CHECKs on rating, occupancy, price |
| inventory | allotment, room_night, hold | hold_log | UNIQUE (room_type_id, stay_date); `0 ≤ reserved_rooms ≤ total_rooms` |
| discovery | listing | — | GIN on amenity_codes |
| booking | booking | booking_log | check_out > check_in; partial index on pending holds |
| payment | payment, refund | payment_log, refund_log | UNIQUE idempotency_key on both live tables |

Migrations: one initial migration per app, plus `catalog/migrations/0002_seed_amenities.py`.

## 13. Configuration

| Variable | `.env.example` (local) | Purpose |
|---|---|---|
| `ENVIRONMENT` | `local` | base default is `production`; `local` enables `/dev/tokens` |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` | dev value, `false` | |
| `API_VERSION` | `v1` | URL prefix |
| `POSTGRES_NAME` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` | `hotel` / `hotel` / `hotel` / `postgres` / `5432` | |
| `REDIS_URL` | `redis://redis:6379/0` | `fakeredis://` in tests |
| `JWT_SIGNING_KEY` | dev base64 (32 bytes) | HS256 key |
| `JWT_ISSUER` / `JWT_AUDIENCE` / `JWT_TTL_SECONDS` | `hotel-booking` / `hotel-booking-api` / `3600` | |
| `PII_ENCRYPTION_KEYS` / `PII_ACTIVE_KEY_ID` | `{"k1": "<dev base64 32 bytes>"}` / `k1` | key ring |
| `PII_BLIND_INDEX_KEY` | dev base64 | HMAC key |
| `IDEMPOTENCY_FINGERPRINT_KEY` | dev base64 | HMAC key |
| `API_ENCRYPTION_MODE` | `optional` | `off` / `optional` / `enforced` |
| `API_RSA_PRIVATE_KEY_PATH` | `secrets/dev/api_rsa_private.pem` | RSA-3072 |
| `RATE_LIMIT_ENABLED` | `true` | |
| `TRUSTED_PROXY_COUNT` | `0` | |
| `DEV_TOKENS_ALLOW_ADMIN` | `true` | local demo only |
| `BOOKING_HOLD_TTL_SECONDS` / `INVENTORY_HORIZON_DAYS` / `MAX_STAY_NIGHTS` | `900` / `365` / `30` | |

`docker-compose.yml` services:
- `postgres` (`postgres:18`, healthcheck) and `redis` (`redis:7-alpine`).
- `app`: built from the Dockerfile on `python:3.13-slim`, with `env_file: [.env.example, {path: .env, required: false}]`. Its entrypoint runs `migrate && seed_demo && gunicorn hotel_booking_service.wsgi -b 0.0.0.0:8000 -w 3`.
- `scheduler`: the same image, running `while true; do python manage.py expire_pending_bookings; python manage.py extend_inventory_horizon; sleep 60; done`.

## 14. Test plan

Unit tests run without a database, using in-memory repositories and fakes. DB and integration tests use Postgres from docker compose (`pytest -m integration`). Redis is `fakeredis` in every test.

| Area | File | Key cases |
|---|---|---|
| Kernel | `libs/hotel-commons/tests/test_domain_kernel.py` | rounding, currency, nights, invalid range → ValidationFailed, StatusCode safe render |
| PII | `…/tests/test_pii.py` | round trip; tamper → error; AAD mismatch (cross-column copy) fails; user input `ENC_x:y` stored encrypted; masks; blind index normalisation |
| JWT | `…/tests/test_jwt.py` | valid; expired; wrong key; `alg=none`; missing aud; oversized sub; no known role |
| Rate limiter | `…/tests/test_rate_limiter.py` | limit → deny with retry_after; window slides; fail-open vs fail-closed policy; spoofed leftmost XFF hop ignored |
| Idempotency | `…/tests/test_idempotency.py` | replay; in progress → 409; mismatch → 422; AppError 4xx stored; 5xx releases; RedisError → view still runs; stored value starts with `ENC_` |
| IDOR, field policies | `…/tests/test_idor.py`, `test_field_level.py` | other guest → 404; {GUEST, OWNER} token cannot cancel another guest's booking at own property → 404; owner masked view; subject sees own PII; restricted write → 403 |
| API encryption | `…/tests/test_api_encryption.py` | round trip; enforced rejects plaintext; tampered payload → 400; replayed key → 400; stale timestamp → 400 |
| Common fields + logs (db) | `…/tests/test_common_fields_and_logs.py` | insert stamps creation_by; update stamps updation_*; one log row per save/update/bulk_create; request_id on logs; raw SQL shows `ENC_` ciphertext in live and log tables; `makemigrations --check` clean |
| Catalog | `catalog/tests/test_property_domain.py` | onboard validation; type rules (villa, hotel rating); add_room_type re-checks rules; duplicate names; standalone and chain accounts share one path |
| Inventory | `inventory/tests/test_inventory_service.py` | hold/confirm/release; idempotent release; horizon edges; missing rows auto-created |
| Concurrency (db) | `tests/integration/test_inventory_concurrency.py` | `@pytest.mark.django_db(transaction=True)`; one room; 10 threads behind `threading.Barrier(10)` call `get_inventory_api().hold(...)`; each closes `django.db.connection` in `finally`; exactly 1 success and 9 `INVENTORY_UNAVAILABLE`; `reserved_rooms == 1`. Second test: overlapping stays (1–3 vs 2–4) race in both orders — no deadlock |
| Pricing | `pricing/tests/test_strategies.py` | weekend nights; occupancy tiers; totals × rooms; registry completeness |
| Discovery | `discovery/tests/test_search_service.py` | each filter; unavailable excluded; price filter; sort orders; paging; registering a new filter needs no service change |
| Booking state machine | `booking/tests/test_state_machine.py` | every allowed transition; every invalid pair → 409 |
| Cancellation policies | `booking/tests/test_cancellation_policies.py` | exactly 24 h / 120 h / 168 h boundaries; non-refundable; after check-in → 422 |
| Booking services | `booking/tests/test_booking_services.py` | create → pay → confirmed; declined stays pending (402 outcome); `now == hold_expires_at` counts as expired; pay after job-expired → EXPIRED; durable replay without Redis; cancel with refund; refund failure rolls back; pay-at-property; expiry job per booking |
| Payment | `payment/tests/test_payment_methods.py`, `test_charge_service.py` | Luhn, brands, VPA, wallet; duplicate key → same payment; refund limits; repr has no card data |
| API end-to-end (db) | `tests/integration/test_booking_flow_api.py` | onboard → search → book → pay → cancel → search shows the room again; book → expire job → room bookable again |
| Architecture (optional) | `tests/architecture/test_module_boundaries.py` | cross-app imports only via `public`; no Django in `domain/`; app graph acyclic |

## 15. Build order and file ownership

| Step | Owner | Delivers | Files only this owner edits |
|---|---|---|---|
| 0. Scaffold | agent S | project package; settings with all 6 apps; root `urls.py` (§10.1); requirements; Dockerfile, compose, `.env.example`, dev keys; pytest config; empty app packages with `apps.py`, `urls.py` stubs | `hotel_booking_service/*`, `requirements*.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `secrets/dev/*`, `pytest.ini`, `conftest.py` |
| 1. Contract core | agent C | `hotel_commons`: §3.2–3.6, §3.9 api helpers + `BaseViewSet`, §3.12 events, `redis/client.py`, `testing/`. Hooks are real but simple: `IdorGuard` complete (no cache), `idempotent` complete with `InMemoryIdempotencyStore` fallback, throttle complete | `libs/hotel-commons/**` |
| 2a. Security track | agent X (after C) | §3.7 JWT, api_crypto, field_level (if not done in C), §3.8 middlewares, §3.10 Redis limiter, §3.11 Redis store, mint_token, system views | `libs/hotel-commons/hotel_commons/{security,middlewares,ratelimit,idempotency}/**` |
| 2b. Inventory → payment → booking | agent A (after C) | §5, §9, §8 incl. views, serializers, commands, tests | `inventory/**`, `payment/**`, `booking/**` |
| 2c. Catalog → pricing → discovery | agent B (after C) | §4 (incl. `seed_demo`), §6, §7 incl. views, serializers, tests | `catalog/**`, `pricing/**`, `discovery/**` |
| 3. Integration | lead | migrations, `manage.py check`, spectacular validation, compose up, e2e tests, README (§16) | `tests/**`, `README.md`, `docs/**` |

Rules:
- The core flows must pass end-to-end with `API_ENCRYPTION_MODE=off` before polishing anything.
- DynamicPricing, the architecture test and `scripts/encrypted_request.py` are built last.

## 16. README contents

1. **Stack note.** Python/Django + DRF was agreed with Rupeek in place of Java/Spring Boot. Include a concept map: Spring interface → ABC in `interfaces/`; `@Service` → `services/`; JPA repository → `repositories/`; `@RestController` → DRF viewset; Spring profile → `ENVIRONMENT`.
2. **Quick start.** `docker compose up --build`, a token from `POST /hotel/v1/dev/tokens` or `manage.py mint_token`, Swagger at `/hotel/v1/docs`, and a curl walkthrough: onboard → search → book → pay → cancel → search again.
3. **Running tests.** `pytest` for unit tests; `pytest -m integration` with compose running.
4. **Assumptions:** A1–A9 from the HLD.
5. **Key design decisions.** The requirement map, modular monolith + hexagonal apps, concurrency strategy, state machines, log-model strategy, PII encryption.
6. **Beyond the brief.** The security features were requested extras. Each is toggleable: `API_ENCRYPTION_MODE=off`, `RATE_LIMIT_ENABLED=false`. Brief §8 lists auth as out of scope; it is minimal here (signed dev tokens).
7. **What I'd do with more time:** HLD §15.
