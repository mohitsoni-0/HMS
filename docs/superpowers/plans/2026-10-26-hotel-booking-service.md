# Hotel Booking Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable Django/DRF hotel booking service (discovery, onboarding, booking, payment, cancellation) on PostgreSQL 18 and Redis, with a shared `hotel_commons` library, exactly as the approved LLD specifies.

**Architecture:** A modular monolith: one Django project with six apps (catalog, inventory, pricing, discovery, booking, payment), each split into domain, interfaces, services and adapters. Apps talk only through `<app>.public`. One database with one schema per app, all on a single connection. Cross-cutting code lives in the installable `libs/hotel-commons`.

**Tech Stack:** Python 3.12 (local venv) / 3.13 (container), Django 5.2, DRF 3.16, drf-spectacular, psycopg 3, PostgreSQL 18, Redis 7 (fakeredis in tests), PyJWT, cryptography, uuid6, pytest + pytest-django.

**Spec:** `docs/HLD.md` and `docs/LLD.md`. **The LLD is the source of every signature, table, route, status code and serializer field.** A task names the LLD section it implements and does not repeat it.

## Global Constraints

- Dependencies are exactly LLD §1 "Pinned dependencies". Local venv: `.venv` (python3.12), already installed.
- Money is integer paisa end to end (`Money(paisa: int)`, `BIGINT *_paisa` columns). Rupee ↔ paisa conversion happens only in `HotelAmountField` (LLD §2.10, §3.14).
- Enums are `SMALLINT` through the model `EnumField`; on the wire they are member names through `HotelEnumField` (LLD §2.11).
- Serializers extend `BaseHotelSerializer` and use only `Hotel*Field`s, with field-level security as field kwargs (LLD §2.12, §3.14).
- Every `db_table = schema_table("<schema>", "<table>")`. Each app's `0001_initial` starts with `create_schema_operation("<schema>")` (LLD §3.15).
- Models use the `CommonX` / `X` / `XLog` triad with `AddCommonField` and `CustomUpdateManager.set_logModel("app.XLog", "app.X")` (LLD §3.5). Repositories follow LLD §2.4 (add = `save(force_insert=True)`; save = load, apply, `save(update_fields=…)`) and §2.5 (savepoint around constraint writes).
- `domain/` imports no Django. Cross-app imports go only through `<app>.public`. Services log ids only; PII and card fields are `field(repr=False)`.
- Routes have no trailing slash and live under `/hotel/v1/` (LLD §10.1). Status codes are `21MMEEE` with `http_status` on the `StatusCode` (LLD §3.2).
- **Trimmed scope** (approved). `ApiEncryptionMiddleware` / `api_crypto` and `DynamicPricing` are design-only: do not build them. `DYNAMIC` is registered as an alias of `WEEKEND`. The README lists both under "Designed, not built".
- Tests use `REDIS_URL=fakeredis://`. Pure unit tests need no database. DB tests are marked `@pytest.mark.django_db` and run against Postgres from docker compose.

## Review Focus

1. A stay ending on the last provisioned night, or a room type provisioned days ago, must still be bookable → Task 2 test `test_hold_creates_missing_rows_within_horizon`.
2. A payment retried with the same Idempotency-Key after Redis forgot it must return the original 200, not 409 → Task 7 test `test_pay_replays_prior_charge_without_redis`.
3. Ten concurrent bookings for the last room → exactly one succeeds and `reserved_rooms == 1` → Task 8 test `test_last_room_race_single_winner`.
4. Amount input `"0.105"`, `-1`, `"abc"` or `NaN` → 400; `4800` serializes as `"4800.00"`; `0.1 + 0.2` style floats never leak → Task 1 test `test_amount_field_conversions`.
5. A token holding both GUEST and OWNER cannot cancel a stranger's booking at its own property → 404 → Task 7 test `test_dual_role_owner_cannot_cancel_strangers_booking`.

---

### Task 0: Project scaffold

**Files:**
- Create: `manage.py`, `hotel_booking_service/{__init__,settings,test_settings,urls,wsgi,asgi}.py`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `conftest.py`, `.gitignore`, `.dockerignore`, `.env.example`, `secrets/dev/api_rsa_private.pem` (RSA-3072, kept for the future encryption feature), `Dockerfile`, `docker-compose.yml`, `local_run.sh`, `run-tests.sh`, and for each app `catalog|inventory|pricing|discovery|booking|payment/{__init__,apps}.py` plus an empty `urls.py` (`urlpatterns = []`).

**Interfaces:**
- Produces:
  - `INSTALLED_APPS` with the 6 apps plus `django.contrib.postgres` and `hotel_commons`.
  - Root `urls.py` exactly as LLD §10.1 (includes may point at empty `urlpatterns`).
  - `test_settings.py`: `REDIS_URL="fakeredis://"`, `ENVIRONMENT="local"`, static 32-byte test keys, `RATE_LIMIT_ENABLED=True`.
  - `pytest.ini` with `DJANGO_SETTINGS_MODULE=hotel_booking_service.test_settings`, `pythonpath = . libs/hotel-commons`, and markers `integration`, `db`.
  - Compose services `postgres:18`, `redis:7-alpine`, `app`, `scheduler` (LLD §13).
  - App configs: `CatalogConfig`, `InventoryConfig`, `PricingConfig`, `DiscoveryConfig`, `BookingConfig`, `PaymentConfig`. Their `ready()` bodies are owned by the app tasks.

- [ ] **Step 1:** Write the files. `settings.py` = `from hotel_commons.settings.base_settings import *` + app list + `ROOT_URLCONF`.
- [ ] **Step 2:** Verify with `.venv/bin/python -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" hotel_booking_service/*.py` → no output.
- [ ] **Step 3:** Commit `chore: project scaffold`.

### Task 1: `hotel_commons` core library (LLD §3.1–§3.6, §3.9, §3.12–§3.15)

**Files** (under `libs/hotel-commons/`):
- Create: `pyproject.toml` and `hotel_commons/{__init__,apps,consts,enum,status_code,exceptions,context}.py`
- `domain/{money,date_range,clock,unit_of_work}.py`, `db/{unit_of_work,schema}.py`
- `models/{base,managers,log_model,fields,pii_companions}.py`
- `security/{__init__ (empty),principal,pi_type,pii,field_level}.py`
- `serializers/{fields,base}.py`, `events/{base,interfaces,in_process}.py`, `redis/client.py`
- `api/{response,exception_handler,pagination,schema}.py`, `logging/{config,filters}.py`, `settings/base_settings.py`
- `testing/{clock,unit_of_work,events,stores,tokens}.py`
- Tests: `tests/test_domain_kernel.py`, `tests/test_pii.py`, `tests/test_serializer_fields.py`

**Interfaces:**
- Produces every name in LLD §3.2–§3.6, §3.9 (`api_response`, `error_body`, `envelope_exception_handler`, `page_data`, `envelope`, `ERROR_ENVELOPE`), §3.12, §3.13, §3.14 (all `Hotel*Field`s, `BaseHotelSerializer`, `BaseHotelModelSerializer`) and §3.15 (`schema_table`, `create_schema_operation`).
- `Money` is `Money(paisa: int, currency="INR")` with `of_rupees`, `rupees`, `times`, `percent`, `divide`, `zero`.
- `base_settings` defines MIDDLEWARE with the Task 1b classes (string paths only) and **omits** `ApiEncryptionMiddleware`.

- [ ] **Step 1: Write failing tests.**
  - `test_money_paisa_arithmetic`: `Money.of_rupees("4800.50").paisa == 480050`; `Money(480050).rupees == Decimal("4800.50")`; `Money(1000).times(Decimal("1.20")).paisa == 1200`; `Money(999).percent(Decimal(50)).paisa == 500` (half-up); `Money.of_rupees("1.005")` raises `ValidationFailed`.
  - `test_date_range_rejects_inverted`: `DateRange(d2, d1)` raises `ValidationFailed`.
  - `test_status_code_safe_render`: `StatusCode(1, "a {x} b").render() == "a {x} b"`.
  - `test_pii_round_trip_and_aad`: `decrypt(encrypt(v, aad="EMAIL|guest_email"), aad="EMAIL|guest_email") == v`; a different aad raises `SecurityFailure`; `encrypt("ENC_x:y", aad=...)` returns a new ciphertext.
  - `test_masks`: `Masker.mask("rahul@gmail.com", PiType.EMAIL) == "r****@gmail.com"`; `Masker.mask("9876543210", PiType.PHONE) == "******3210"`.
  - `test_amount_field_conversions`: `HotelAmountField().to_internal_value("4800.5") == 480050`; `to_representation(480000) == "4800.00"`; `"0.105"`, `"-1"`, `"abc"`, `"NaN"` each raise `ValidationError`.
  - `test_enum_field_conversions`: `HotelEnumField(BookingTypeLike).to_internal_value("PREPAID")` is the member; `to_representation(1) == "PREPAID"`; `to_internal_value(1)` raises.
  - `test_field_security_restrict_and_mask`: a serializer with `HotelCharField(restrict_roles=[Role.ADMIN])` raises `Forbidden` for a GUEST principal sending the field; `HotelEncryptedCharField(read_roles=[Role.ADMIN], subject_attr="guest_id", mask_as=PiType.EMAIL)` returns plaintext for the subject and a mask for an OWNER.
- [ ] **Step 2:** Run `.venv/bin/pytest libs/hotel-commons/tests -q` → the tests fail (ImportError).
- [ ] **Step 3:** Implement LLD §3.1–§3.6, §3.9, §3.12–§3.15. `AddCommonField.save` decides insert vs update with `_state.adding`; the `EncryptedTextField` AAD is `f"{pi_type.name}|{self.name}"`.
- [ ] **Step 4:** Run `.venv/bin/pytest libs/hotel-commons/tests -q` → PASS.
- [ ] **Step 5:** Commit `feat(commons): core kernel, models, PII, serializers`.

### Task 1b: `hotel_commons` security and API pipeline (LLD §3.7 minus api_crypto, §3.8 rows 2/3/5, §3.9 views, §3.10, §3.11)

**Files** (under `libs/hotel-commons/hotel_commons/`):
- Create: `security/{jwt_tokens,idor,permissions,client_ip}.py`
- `middlewares/{__init__,request_context,exception_handling,authentication}.py`
- `api/{authentication,views,throttling,system_views,error_views,urls}.py`
- `ratelimit/{__init__,interfaces,redis_sliding_window}.py`, `idempotency/{__init__,interfaces,redis_store,decorators}.py`
- `management/__init__.py`, `management/commands/{__init__,mint_token}.py`
- Tests: `libs/hotel-commons/tests/test_jwt.py`, `test_rate_limiter.py`, `test_idempotency.py`, `test_idor.py`

**Interfaces:**
- Consumes the Task 1 names exactly as in LLD §3.2–§3.4 and §3.6 (code against the LLD; Task 1 runs in parallel).
- Produces:
  - `JwtTokenService`, `get_token_service`, `IdorRule`, `IdorGuard`, `Ownership`, `OwnershipResolver`, `ownership_registry`, `require_roles`, `IsAuthenticatedPrincipal`, `client_ip`.
  - `BaseViewSet` (with `permission_classes_by_action`, `throttle_scope_by_action`, `idor_rules`), `PrincipalAuthentication`, `RedisRateLimitThrottle`, `idempotent(scope, *, required=True, ...)`.
  - `HealthView`, `DevTokenView`, `not_found`, `server_error`, and `hotel_commons.api.urls` (health, dev/tokens when local, schema, docs). No `PublicKeyView` (payload encryption is design-only).

- [ ] **Step 1: Write failing tests.**
  - `test_jwt_valid_expired_wrong_key_alg_none_oversized_sub`
  - `test_rate_limiter_denies_after_limit_and_reports_retry_after` (fakeredis)
  - `test_rate_limiter_fail_closed_policy_denies_on_redis_error`
  - `test_client_ip_ignores_spoofed_leftmost_hop`
  - `test_idempotent_replay_in_progress_mismatch_and_5xx_release` (InMemoryIdempotencyStore)
  - `test_idempotent_runs_view_when_store_raises_redis_error`
  - `test_idor_guard_relations` (GUEST+OWNER principal, `relations={"guest"}`, resource owned via owner_user_ids only → `NotFound`)
- [ ] **Step 2:** Run `.venv/bin/pytest libs/hotel-commons/tests -q -k "jwt or rate or idempot or idor or client_ip"` → FAIL.
- [ ] **Step 3:** Implement per LLD. Run the Lua script with Redis `TIME`; if fakeredis can't, use the `ARGV[4]` fallback from LLD §3.10.
- [ ] **Step 4:** Run the same command → PASS.
- [ ] **Step 5:** Commit `feat(commons): JWT, IDOR, rate limiter, idempotency, middlewares`.

### Task 2: `inventory` app (LLD §5)

**Files:**
- Create: `inventory/{enums,constants,status_code,container,urls}.py`
- `inventory/domain/{allotment,room_night,hold}.py`, `inventory/interfaces/repositories.py`, `inventory/models/inventory_models.py`
- `inventory/repositories/{allotment_repository,room_night_repository,hold_repository,mappers}.py`
- `inventory/services/inventory_service.py`, `inventory/public/{api,dto,enums}.py`
- `inventory/management/commands/extend_inventory_horizon.py`, `inventory/migrations/0001_initial.py`
- Tests: `inventory/tests/{fakes,test_inventory_service}.py`

**Interfaces:**
- Produces `get_inventory_api() -> InventoryAPI` with:
  - `provision(room_type_id, total_rooms)`
  - `extend_horizon() -> int`
  - `hold(*, room_type_id, stay, rooms, reference, expires_at) -> HoldView`
  - `confirm_hold(hold_id)`
  - `release_hold(hold_id, reason: ReleaseReason)`
  - `availability(room_type_ids, stay, rooms) -> dict[UUID, int]`
  - `occupancy(room_type_id, dates) -> dict[date, Decimal]`

- [ ] **Step 1: Write failing tests** (in-memory fakes, FixedClock):
  - `test_hold_then_release_restores_availability`
  - `test_release_is_idempotent`
  - `test_hold_rejects_when_any_night_full`
  - `test_hold_creates_missing_rows_within_horizon`
  - `test_stay_outside_horizon_is_rejected`
  - `test_confirm_released_hold_conflicts`
- [ ] **Step 2:** Run `.venv/bin/pytest inventory -q` → FAIL.
- [ ] **Step 3:** Implement LLD §5. Tables go in schema `inventory`; `room_night` uses `fillfactor=80` via a `RunSQL` in 0001; the CHECK constraints are as specified; repositories follow LLD §2.4/§2.5. Write `0001_initial.py` by hand: `create_schema_operation("inventory")` + CreateModel.
- [ ] **Step 4:** Run `.venv/bin/pytest inventory -q` → PASS.
- [ ] **Step 5:** Commit `feat(inventory): allotment, nights, holds`.

### Task 3: `payment` app (LLD §9)

**Files:**
- Create: `payment/{enums,status_code,container,urls,apps}.py` (`apps.ready` registers the `payment` IDOR resolver)
- `payment/domain/{instrument,methods,payment,refund,errors}.py`, `payment/interfaces/{repositories,gateway}.py`
- `payment/adapters/mock_gateway.py`, `payment/models/payment_models.py`
- `payment/repositories/{payment_repository,refund_repository,mappers}.py`
- `payment/services/{charge_service,refund_service,query_service}.py`, `payment/public/{api,dto,enums}.py`
- `payment/serializers/payment_serializers.py`, `payment/views/payment_views.py`, `payment/migrations/0001_initial.py`
- Tests: `payment/tests/{fakes,test_payment_methods,test_charge_service}.py`

**Interfaces:**
- Produces `get_payment_api() -> PaymentAPI` with:
  - `find_by_idempotency_key(key) -> PaymentDTO | None`
  - `charge(ChargeCommand) -> PaymentDTO`
  - `refund(RefundCommand) -> RefundResultDTO` (raises `UpstreamFailure(REFUND_FAILED)`)
  - `get_payment(payment_id) -> PaymentDTO`
- All amounts are `Money` (paisa).

- [ ] **Step 1: Write failing tests:**
  - `test_card_luhn_brand_expiry_cvv` (4242424242424242 → VISA, display "VISA •••• 4242"; bad Luhn → `ValidationFailed`)
  - `test_upi_and_wallet_validation`
  - `test_charge_declined_for_card_ending_0002`
  - `test_same_idempotency_key_returns_same_payment_single_gateway_call`
  - `test_refund_cannot_exceed_payment`
  - `test_refund_failure_raises_upstream_failure`
  - `test_charge_command_repr_hides_card_details`
- [ ] **Step 2:** Run `.venv/bin/pytest payment -q` → FAIL.
- [ ] **Step 3:** Implement LLD §9. Schema `payment`; `instrument_ref` is an `EncryptedTextField(pi_type=OTHER)`; route `payments/<uuid:payment_id>`.
- [ ] **Step 4:** Run `.venv/bin/pytest payment -q` → PASS.
- [ ] **Step 5:** Commit `feat(payment): methods, mock gateway, charge and refund`.

### Task 4: `catalog` app (LLD §4)

**Files:**
- Create: `catalog/{enums,status_code,container,urls,apps}.py` (`ready` registers the `owner_account` and `property` resolvers)
- `catalog/domain/{address,owner_account,property,room_type,property_rules}.py`, `catalog/interfaces/{repositories,ports}.py`
- `catalog/adapters/inventory_provisioning.py`, `catalog/models/catalog_models.py`, `catalog/repositories/*.py`
- `catalog/services/{commands,owner_account_service,onboarding_service,room_type_service,query_service}.py`
- `catalog/public/{api,dto,events,enums}.py`, `catalog/serializers/*.py`, `catalog/views/*.py`
- `catalog/management/commands/seed_demo.py`, `catalog/migrations/{0001_initial,0002_seed_amenities}.py`
- Tests: `catalog/tests/{fakes,test_property_domain,test_onboarding_service}.py`

**Interfaces:**
- Produces:
  - `catalog.public.enums`: `PropertyType`, `PricingStrategyCode`, `CancellationPolicyCode`, `AmenityCategory`.
  - `catalog.public.events`: `PropertyOnboarded`, `RoomTypeAdded`.
  - `catalog.public.dto`: `PropertySnapshot`, `RoomTypeSummary`, `RoomTypeSnapshot`.
  - `get_catalog_api() -> CatalogAPI` with `room_type_snapshot(room_type_id)` and `owner_user_id(account_id)`.
- Consumes `get_inventory_api().provision`.

- [ ] **Step 1: Write failing tests:**
  - `test_onboard_requires_room_type`
  - `test_hotel_requires_star_rating`
  - `test_villa_entire_place_rule_on_add_room_type`
  - `test_duplicate_room_type_name_case_insensitive`
  - `test_unknown_amenities_rejected`
  - `test_standalone_and_chain_use_same_path` (one account with 1 property and one with 2, both via `PropertyOnboardingService`)
  - `test_onboarding_provisions_inventory_and_publishes_event` (RecordingEventPublisher + fake provisioning port)
- [ ] **Step 2:** Run `.venv/bin/pytest catalog -q` → FAIL.
- [ ] **Step 3:** Implement LLD §4, routes as LLD §10.2 rows 4–9, serializers as §10.3. Base price is `HotelAmountField` (paisa inside). `seed_demo` is idempotent.
- [ ] **Step 4:** Run `.venv/bin/pytest catalog -q` → PASS.
- [ ] **Step 5:** Commit `feat(catalog): owners, properties, room types, onboarding`.

### Task 5: `pricing` app (LLD §6, trimmed)

**Files:**
- Create: `pricing/domain/{quote,strategies,registry}.py`, `pricing/interfaces/ports.py`, `pricing/adapters/inventory_occupancy.py`
- `pricing/services/pricing_service.py`, `pricing/public/{api,dto}.py`, `pricing/container.py`, `pricing/status_code.py`
- Tests: `pricing/tests/test_strategies.py`

**Interfaces:**
- Produces `get_pricing_api() -> PricingAPI` with `quote(PricingRequest) -> QuoteDTO` and `quote_many(requests) -> dict[UUID, QuoteDTO]`.
- `PricingRequest(room_type_id, base_price: Money, strategy: PricingStrategyCode, stay: DateRange, rooms: int)` is re-exported from `pricing.public.dto`.

- [ ] **Step 1: Write failing tests:**
  - `test_standard_every_night_base`
  - `test_weekend_friday_saturday_times_1_20`, e.g. base ₹4,000 Fri night = 480000 paisa
  - `test_total_is_per_room_total_times_rooms`
  - `test_every_strategy_code_registered` (DYNAMIC → WEEKEND alias)
- [ ] **Step 2:** Run `.venv/bin/pytest pricing -q` → FAIL.
- [ ] **Step 3:** Implement. Rounding per night is `ROUND_HALF_UP` to whole paisa.
- [ ] **Step 4:** Run `.venv/bin/pytest pricing -q` → PASS.
- [ ] **Step 5:** Commit `feat(pricing): standard and weekend strategies`.

### Task 6: `discovery` app (LLD §7)

**Files:**
- Create: `discovery/domain/{criteria,listing,offers,filters,sorting}.py`, `discovery/interfaces/{repositories,ports}.py`
- `discovery/adapters/{inventory_availability,pricing_quotes}.py`, `discovery/models/listing_models.py`, `discovery/repositories/listing_repository.py`
- `discovery/services/{search_service,listing_projector}.py`, `discovery/serializers/search_serializers.py`, `discovery/views/search_views.py`
- `discovery/{container,urls,apps,status_code}.py` (`ready` subscribes the projector to `PropertyOnboarded` and `RoomTypeAdded`), `discovery/migrations/0001_initial.py`
- Tests: `discovery/tests/{fakes,test_search_service}.py`

**Interfaces:**
- Consumes the `catalog.public.events`/`dto` classes, `get_inventory_api().availability` and `get_pricing_api().quote_many`.
- Produces `GET /hotel/v1/discovery/search`.

- [ ] **Step 1: Write failing tests:**
  - `test_city_and_occupancy_filters`
  - `test_amenities_contains_all`
  - `test_unavailable_room_types_excluded`
  - `test_price_range_uses_average_nightly`
  - `test_sort_price_and_rating`
  - `test_new_filter_registers_without_service_change`
- [ ] **Step 2:** Run `.venv/bin/pytest discovery -q` → FAIL.
- [ ] **Step 3:** Implement LLD §7 and the §10.3 `SearchQuerySerializer`/`SearchPageSerializer`. Schema `discovery`; GIN index on `amenity_codes`.
- [ ] **Step 4:** Run `.venv/bin/pytest discovery -q` → PASS.
- [ ] **Step 5:** Commit `feat(discovery): listing projection and filterable search`.

### Task 7: `booking` app (LLD §8)

**Files:**
- Create: `booking/{enums,constants,status_code,container,urls,apps}.py` (`ready` registers the `booking` resolver)
- `booking/domain/{booking,state_machine,booking_types,cancellation,value_objects}.py`, `booking/interfaces/{repositories,ports}.py`
- `booking/adapters/{catalog_adapter,pricing_adapter,inventory_adapter,payment_adapter}.py`
- `booking/models/booking_models.py`, `booking/repositories/{booking_repository,mappers}.py`
- `booking/services/{commands,settings,create_booking,pay_booking,cancel_booking,expire_bookings,query_service}.py`
- `booking/public/events.py`, `booking/serializers/booking_serializers.py`, `booking/views/booking_views.py`
- `booking/management/commands/expire_pending_bookings.py`, `booking/migrations/0001_initial.py`
- Tests: `booking/tests/{fakes,test_state_machine,test_cancellation_policies,test_booking_services,test_booking_api}.py`

**Interfaces:**
- Consumes `get_catalog_api()`, `get_pricing_api()`, `get_inventory_api()` and `get_payment_api()` (signatures in Tasks 2–5, LLD §8.3 adapters).
- Produces the routes in LLD §10.2 rows 11–15.

- [ ] **Step 1: Write failing tests:**
  - `test_every_valid_transition` and `test_invalid_transitions_raise_409`
  - `test_cancellation_tier_boundaries` (exactly 24 h → 100 % FLEXIBLE; 23 h 59 m → 50 %; MODERATE 120 h boundary; NON_REFUNDABLE → 0)
  - `test_create_pay_confirm`
  - `test_declined_payment_keeps_pending_outcome`
  - `test_hold_expired_at_exact_boundary` (now == hold_expires_at → EXPIRED)
  - `test_pay_after_job_expired_returns_expired`
  - `test_pay_replays_prior_charge_without_redis`
  - `test_cancel_confirmed_refunds_by_policy`
  - `test_refund_failure_rolls_back_cancel`
  - `test_pay_at_property_confirmed_without_payment`
  - `test_guest_capacity_exceeded`
  - `test_expire_job_handles_each_booking_separately`
  - API test (db, APIClient): `test_dual_role_owner_cannot_cancel_strangers_booking` (404), `test_owner_sees_masked_guest_pii`, `test_guest_sees_own_pii`
- [ ] **Step 2:** Run `.venv/bin/pytest booking -q -m "not db"` → FAIL.
- [ ] **Step 3:** Implement LLD §8. The pay view returns outcomes (200/402/409) with `data={booking, payment}`; `@idempotent("booking_pay")` is required on pay.
- [ ] **Step 4:** Run `.venv/bin/pytest booking -q -m "not db"` → PASS.
- [ ] **Step 5:** Commit `feat(booking): lifecycle, pay, cancel, expiry`.

### Task 8: Integration, end-to-end, README (lead)

**Files:**
- Create: `tests/integration/test_booking_flow_api.py`, `tests/integration/test_inventory_concurrency.py`, `README.md` (LLD §16 plus a "Designed, not built" section)

- [ ] **Step 1:** Run `.venv/bin/python manage.py check` and `.venv/bin/python manage.py makemigrations --check --dry-run` → no errors, no changes.
- [ ] **Step 2:** `docker compose up -d postgres redis`, then `.venv/bin/python manage.py migrate`, then `.venv/bin/python manage.py seed_demo` → success.
- [ ] **Step 3: Write e2e tests:**
  - `test_onboard_search_book_pay_cancel_search` (search shows the room again after cancel)
  - `test_last_room_race_single_winner` (`django_db(transaction=True)`, `threading.Barrier(10)`, `connection.close()` in `finally`; exactly one winner, `reserved_rooms == 1`)
  - `test_overlapping_stays_no_deadlock`
- [ ] **Step 4:** Run `.venv/bin/pytest -q` → all pass.
- [ ] **Step 5:** `docker compose up --build -d`, then `curl -s localhost:8000/hotel/v1/health` → `{"status": 2100000, ... "database": "ok"}`.
- [ ] **Step 6:** Commit `test: end-to-end flows and concurrency; docs: README`.
