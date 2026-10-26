# Hotel Booking Service — High-Level Design (HLD)

| | |
|---|---|
| Status | Draft for review |
| Date | 2026-10-26 |
| Stack | Python 3.13 · Django 5.2 LTS · Django REST Framework 3.16 · PostgreSQL 18 · Redis 7 |
| Companion | [LLD.md](LLD.md) — classes, tables, endpoints, test plan |

## 1. Goal and scope

Backend for a hotel booking platform (Rupeek SDE-3 machine-coding brief, Question A). Guests search properties and book a room type for a date range. Owners onboard one or many properties. A booking is paid for and can be cancelled under a refund policy.

The design optimises for what the brief weights highest: interfaces at the right seams, accurate domain modelling with explicit state machines, correctness under concurrency, and code another engineer can extend.

### 1.1 In scope

- **Core flows:** discovery, property onboarding, booking, payment, cancellation.
- **Bonus items:** concurrency-safe inventory, pluggable and dynamic pricing, payment idempotency, OpenAPI docs.
- **Platform features in the shared library:** signed-JWT identity, IDOR protection, field-level security, API payload encryption, a Redis rate limiter, idempotency, the audit log-model strategy, common audit fields (`AddCommonField`), and PII encryption at rest.

### 1.2 Out of scope

Login and user management (dev tokens come from a management command), UI, a real payment gateway (mocked behind an interface), taxes, multi-currency, overbooking, physical room assignment, and production deployment.

### 1.3 Assumptions

| # | Assumption |
|---|---|
| A1 | Inventory is sold per **room type per night**. Rooms inside a room type are interchangeable; a physical room number is assigned at check-in (out of scope). |
| A2 | A stay is `[check_in, check_out)`: the check-out night is not charged. Stays are 1–30 nights and may start from today up to 365 days ahead. |
| A3 | One booking is one room type × `rooms` identical rooms, with `guests ≤ rooms × max_occupancy`. |
| A4 | Single currency (INR). Prices are per room per night, taxes included. |
| A5 | A prepaid booking holds inventory for 15 minutes (configurable). Unpaid holds expire and release inventory. |
| A6 | A declined payment leaves the booking `PENDING_PAYMENT`, so the guest can retry with another method until the hold expires. |
| A7 | Refunds follow the cancellation policy **snapshotted at booking time**; later property edits never change an existing booking's terms. |
| A8 | Every property belongs to exactly one owner account, and every owner account has one owning user. An independent hotelier is an account with one property — the same code path as a chain. |
| A9 | Callers present a signed JWT carrying `sub` and `roles` (GUEST, OWNER, ADMIN). Search, property details and amenities are public. |

## 2. Requirements → design map

| Brief requirement | Where | Mechanism |
|---|---|---|
| 3.1 Search by city/locality, dates, guests | `discovery` | Listing read model, filtered, then live availability (`inventory`) and quotes (`pricing`) |
| 3.1 Add filters without reworking search | `discovery.domain.filters` | `SearchFilter` interface plus registry; a new filter is one new class |
| 3.1 Only available properties | `inventory` | One aggregate SQL over per-night rows for all candidate room types |
| 3.2 Onboard property, room types, amenities, pricing | `catalog` | `PropertyOnboardingService` |
| 3.2 Standalone vs multi-property owner | `catalog.domain` | `OwnerAccount` 1—N `Property`; standalone = account with one property |
| 3.3 Book a room type; prevent double booking | `booking` + `inventory` | Ordered row locks + DB `CHECK` constraint on per-night counters |
| 3.3 Clear lifecycle | `booking.domain.state_machine` | Table-driven state machine; invalid transitions → 409 |
| 3.4 Multiple payment methods | `payment.domain.methods` | `PaymentMethodHandler` strategy registry (CARD, UPI, WALLET) |
| 3.4 Payment outcome drives booking state | `booking.services.pay_booking` | Success → `CONFIRMED`; decline → stays `PENDING_PAYMENT` |
| 3.5 Pluggable cancellation and refund | `booking.domain.cancellation` | `CancellationPolicy` interface; tiered policies chosen per property |
| 3.5 Release inventory on cancel | `inventory` | Idempotent `release_hold` |
| 4 Repositories behind interfaces | every app | `interfaces/` ABCs; Django and in-memory implementations |
| 4 Mock third parties | `payment.adapters` | `PaymentGateway` interface; `MockPaymentGateway` |
| 7 Concurrent bookings | `inventory`, `booking` | §9 |
| 7 Pluggable / dynamic pricing | `pricing` | `PricingStrategy` registry: STANDARD, WEEKEND, DYNAMIC (occupancy surge) |
| 7 Payment idempotency | shared library + `payment` | `Idempotency-Key` in Redis + unique key in Postgres + idempotent gateway call |
| 7 OpenAPI | project | drf-spectacular → `/hotel/v1/docs/` |

## 3. Architecture

### 3.1 Style

A **modular monolith**: one Django project and one deployable, split into six apps, one per bounded context. Each app is layered hexagonally inside — pure domain, interfaces (ports), use-case services, and adapters (ORM, gateway, other apps). Apps reach each other only through a small `public` package. A shared library, `hotel_commons`, holds cross-cutting code and nothing hotel-specific.

Why this shape:
- The brief asks for domain logic kept apart from framework and persistence, and repositories behind interfaces. Hexagonal layering is exactly that.
- One deployable keeps the review simple — one `docker compose up` — and keeps booking-then-payment a local transaction instead of a saga.
- App boundaries, UUID references across apps and no cross-app foreign keys mean any app can later become a service by replacing its in-process adapter with an HTTP client implementing the same interface (§14).

### 3.2 Container view

```mermaid
flowchart LR
    client["API client / Swagger UI"] -->|"HTTPS + JSON<br/>Bearer JWT, optional encrypted payload"| api
    subgraph svc["hotel-booking-service (one Django deployable)"]
        api["DRF API layer<br/>views + serializers"] --> apps
        subgraph apps["Bounded-context apps"]
            catalog["catalog"]
            discovery["discovery"]
            booking["booking"]
            inventory["inventory"]
            pricing["pricing"]
            payment["payment"]
        end
        commons[["hotel_commons<br/>shared library"]]
    end
    apps --> pg[("PostgreSQL 18")]
    commons --> redis[("Redis 7<br/>rate limit, idempotency, replay guard")]
    payment --> gw[["Mock payment gateway"]]
```

### 3.3 Bounded contexts

| App | Responsibility | Owns (tables) | Public API (`<app>.public`) | Depends on |
|---|---|---|---|---|
| `catalog` | Owner accounts, properties, room types, amenities, commercial settings (pricing strategy, cancellation policy) | owner_account, property, room_type (+ logs), amenity | `CatalogAPI`, events `PropertyOnboarded`, `RoomTypeAdded`, published enums | inventory |
| `inventory` | Per-night allotment per room type; holds | allotment, room_night, hold (+ log) | `InventoryAPI`: provision, extend_horizon, hold, confirm, release, availability, occupancy | — |
| `pricing` | Nightly prices through pluggable strategies | — | `PricingAPI`: quote, quote_many | inventory, catalog (enums) |
| `discovery` | Search read model and filter pipeline | listing | — (HTTP only) | catalog (events), inventory, pricing |
| `booking` | Booking aggregate, lifecycle, booking types, cancellation policies; orchestrates pay and cancel | booking (+ log) | events only (no other app calls booking) | catalog, pricing, inventory, payment |
| `payment` | Payments, refunds, method strategies, gateway | payment, refund (+ logs) | `PaymentAPI`: find_by_idempotency_key, charge, refund, get_payment | — |

### 3.4 Context map

Arrows point from the importing app to the `public` package it uses. The graph is acyclic, and an architecture test fails the build on a cycle or on any cross-app import that bypasses `public`.

```mermaid
flowchart TD
    booking --> catalog
    booking --> pricing
    booking --> inventory
    booking --> payment
    discovery -->|"subscribes to events"| catalog
    discovery --> pricing
    discovery --> inventory
    catalog --> inventory
    pricing --> inventory
    pricing -->|"enums only"| catalog
```

### 3.5 Inside an app

```mermaid
flowchart LR
    views["views/ + serializers/<br/>(DRF, thin)"] --> container["container.py<br/>(composition root)"]
    container --> services["services/<br/>use cases"]
    services --> domain["domain/<br/>entities, value objects,<br/>state machines, policies"]
    services --> interfaces["interfaces/<br/>repository + port ABCs"]
    repos["repositories/<br/>Django ORM"] -.implements.-> interfaces
    adapters["adapters/<br/>other apps' public API,<br/>gateways"] -.implements.-> interfaces
    repos --> models["models/<br/>Common / Main / Log"]
    public["public/<br/>facade + DTOs + events"] --> container
```

| Layer | May import | Must not import |
|---|---|---|
| `domain/` | stdlib, `hotel_commons.domain`, `hotel_commons.exceptions`, own `enums`/`status_code`, other apps' `public` enums | Django, DRF, models |
| `interfaces/` | own domain | Django, DRF |
| `services/` | own domain, own interfaces, `hotel_commons` (unit of work, events, exceptions) | models, views, other apps |
| `repositories/`, `adapters/` | models, Django, other apps' `public` | views |
| `views/`, `serializers/` | `container`, DRF, `hotel_commons.api` / `security` | models, repositories |
| `public/` | `container`, own DTOs | models |

`container.py` builds each service with Django repositories and in-process adapters. Unit tests build the same services with in-memory repositories and fakes, which proves the persistence and the other contexts are swappable.

## 4. Shared library — `hotel_commons`

An installable package in `libs/hotel-commons` (`pip install -e`), playing the same role as `kuhoo-library`: every app — and every future service — imports base settings, middlewares, model base classes and security primitives from it. It contains nothing hotel-specific.

| Module | Provides |
|---|---|
| `settings.base_settings` | DRF config, middleware order, logging, Postgres and Redis from env |
| `models` | `AddCommonField`, the log-model strategy (`CustomUpdateManager.set_logModel`, `add_log_model`), `EnumField`, `EncryptedTextField`, `BlindIndexField` |
| `security` | `Principal` and roles, JWT `TokenService`, PII cipher / blind index / masking, IDOR guard, field-level security, API payload cipher, permissions |
| `middlewares` | request context, JWT authentication, API payload encryption, last-resort exception handling |
| `ratelimit` | `RateLimiter` interface, Redis sliding-window implementation (atomic Lua), DRF throttle |
| `idempotency` | `IdempotencyStore` interface, Redis store, `@idempotent` view decorator |
| `events` | `DomainEvent`, `EventPublisher` interface, in-process bus |
| `domain` | `Money`, `DateRange`, `Clock`, `UnitOfWork` |
| `api` | response envelope, DRF exception handler, `BaseViewSet`, pagination |
| `exceptions`, `status_code`, `enum`, `context` | error hierarchy, status-code catalogue, labelled enums, request-scoped context (`ContextVar`) |
| `testing` | fakes (clock, unit of work, event recorder, in-memory stores), token factory |

## 5. Request pipeline

```mermaid
flowchart TD
    A["Request"] --> B["RequestContextMiddleware<br/>X-Request-ID, ContextVar"]
    B --> C["ExceptionHandlingMiddleware<br/>last-resort 500 envelope"]
    C --> D["ApiEncryptionMiddleware<br/>decrypt body / encrypt response"]
    D --> E["JWTAuthenticationMiddleware<br/>verify signature, build Principal"]
    E --> F["BaseViewSet.initial()"]
    F --> G["1. Rate limit (Redis)"]
    G --> H["2. Role permissions"]
    H --> I["3. IDOR ownership check"]
    I --> J["@idempotent (Redis)"]
    J --> K["Serializer: field-level security + validation"]
    K --> L["Application service (unit of work)"]
```

`BaseViewSet.initial()` reorders DRF's default (authenticate → permissions → throttle) to throttle before permissions, so a throttled caller never triggers ownership lookups.

## 6. Key flows

### 6.1 Onboard a property

```mermaid
sequenceDiagram
    actor O as Owner
    participant V as catalog view
    participant S as PropertyOnboardingService
    participant R as PropertyRepository
    participant I as InventoryAPI
    participant B as EventBus
    participant D as discovery ListingProjector
    O->>V: POST /catalog/owner-accounts/{id}/properties
    V->>V: role OWNER or ADMIN, IDOR on account, validate payload
    V->>S: onboard(account_id, draft)
    S->>S: Property.onboard() with property-type rules
    S->>R: add(property + room types), log rows written
    S->>I: provision(room_type_id, total_rooms) for 365 nights
    S->>B: publish PropertyOnboarded
    B->>D: upsert listings in the same transaction
    V-->>O: 201 property
```

### 6.2 Search

```mermaid
sequenceDiagram
    actor G as Guest
    participant V as discovery view
    participant S as SearchService
    participant L as ListingRepository
    participant I as InventoryAPI
    participant P as PricingAPI
    G->>V: GET /discovery/search?city=...&check_in=...&guests=...
    V->>S: search(criteria)
    S->>S: INDEX-stage filters become predicates
    S->>L: find(predicates) gives candidate room types
    S->>I: availability(room_type_ids, stay, rooms) in one SQL
    S->>P: quote_many(available room types)
    S->>S: OFFER-stage filters (price), group by property, sort, page
    V-->>G: 200 page of properties with available room offers
```

### 6.3 Create a booking (hold)

```mermaid
sequenceDiagram
    actor G as Guest
    participant V as booking view
    participant S as CreateBookingService
    participant C as CatalogPort
    participant P as PricingPort
    participant I as InventoryPort
    participant R as BookingRepository
    G->>V: POST /bookings
    V->>S: execute(command)
    S->>C: room_type_info(room_type_id)
    S->>P: quote(room type, stay, rooms)
    S->>S: Booking.request() validates and snapshots price and policy
    S->>I: hold(room_type, stay, rooms, expires_at)
    Note over I: SELECT ... ORDER BY stay_date FOR UPDATE,<br/>check availability, increment counters,<br/>CHECK constraint as last line of defence
    S->>R: add(booking)
    V-->>G: 201 PENDING_PAYMENT, total, hold_expires_at
```

### 6.4 Pay

```mermaid
sequenceDiagram
    actor G as Guest
    participant V as booking view
    participant S as PayBookingService
    participant R as BookingRepository
    participant Pay as PaymentPort
    participant I as InventoryPort
    G->>V: POST /bookings/{id}/payments + Idempotency-Key
    V->>V: @idempotent: replay, 409 in progress, or 422 key reuse
    V->>S: execute(command)
    S->>R: get_for_update(booking) row lock
    S->>Pay: find_charge(pay:booking:key) durable replay, works even if Redis lost the key
    alt hold expired
        S->>S: booking.expire()
        S->>I: release_hold()
        V-->>G: 409 BOOKING_EXPIRED (expiry committed)
    else payable
        S->>Pay: charge(amount, method, details, key)
        Note over Pay: method handler validates,<br/>Payment INITIATED, gateway call,<br/>SUCCEEDED or FAILED
        alt succeeded
            S->>S: booking.confirm_payment()
            S->>I: confirm_hold()
            V-->>G: 200 CONFIRMED
        else declined
            S->>S: booking.record_payment_failure()
            V-->>G: 402 PAYMENT_DECLINED, booking still PENDING_PAYMENT
        end
    end
    V->>V: store final response under the Idempotency-Key
```

### 6.5 Cancel

```mermaid
sequenceDiagram
    actor G as Guest
    participant S as CancelBookingService
    participant R as BookingRepository
    participant Pol as CancellationPolicyRegistry
    participant I as InventoryPort
    participant Pay as PaymentPort
    G->>S: POST /bookings/{id}/cancel
    S->>R: get_for_update(booking)
    S->>Pol: get(booking.cancellation_policy).evaluate(paid, check-in time, now)
    S->>S: booking.cancel(refund decision)
    S->>I: release_hold() idempotent
    opt refund amount > 0
        S->>Pay: refund(payment_id, amount, key = refund:booking_id)
        Note over S,Pay: a gateway refund failure rolls the whole cancel back (502),<br/>the booking stays CONFIRMED and the client retries
    end
    S-->>G: 200 CANCELLED with refund amount
```

### 6.6 Expire unpaid holds and extend the inventory horizon

A `scheduler` container in docker compose runs two commands every 60 seconds:
- `manage.py expire_pending_bookings` reads the ids of `PENDING_PAYMENT` bookings whose `hold_expires_at <= now`. It then handles each booking in **its own transaction**: lock it with `FOR UPDATE SKIP LOCKED`, expire it, release its hold.
- `manage.py extend_inventory_horizon` keeps per-night rows provisioned up to today + 365 days.

The pay flow also expires lazily (§6.4), so correctness never depends on the job's timing. In production the same commands run from cron or Celery beat.

## 7. Lifecycles

### 7.1 Booking

```mermaid
stateDiagram-v2
    [*] --> PENDING_PAYMENT : create (PREPAID)
    [*] --> CONFIRMED : create (PAY_AT_PROPERTY)
    PENDING_PAYMENT --> CONFIRMED : payment succeeded
    PENDING_PAYMENT --> PENDING_PAYMENT : payment declined
    PENDING_PAYMENT --> CANCELLED : guest cancels, nothing charged
    PENDING_PAYMENT --> EXPIRED : hold expired
    CONFIRMED --> CANCELLED : cancel, refund per policy
    CANCELLED --> [*]
    EXPIRED --> [*]
```

### 7.2 Payment and inventory hold

```mermaid
stateDiagram-v2
    state Payment {
        [*] --> INITIATED
        INITIATED --> SUCCEEDED
        INITIATED --> FAILED
        SUCCEEDED --> PARTIALLY_REFUNDED
        SUCCEEDED --> REFUNDED
        PARTIALLY_REFUNDED --> REFUNDED
    }
    state Hold {
        [*] --> HELD
        HELD --> CONFIRMED : payment succeeded
        HELD --> RELEASED : cancelled or expired
        CONFIRMED --> RELEASED : cancelled
    }
```

## 8. Data architecture

### 8.1 Ownership

- Each app owns its tables, prefixed with its label (`booking_booking`, `payment_refund`).
- No foreign keys across apps — cross-context references are UUID columns — so each app's tables can move to their own database when split. Foreign keys inside an app are normal.
- Primary keys are UUIDv7: time-ordered (good B-tree locality) and unguessable (a second layer against ID enumeration behind IDOR checks).

### 8.2 Common fields — `AddCommonField`

Every table inherits `status` (1 active, 0 inactive — soft delete), `creation_date`, `creation_by`, `updation_date` and `updation_by`. They are stamped from the caller's principal (read from a `ContextVar`) on every write path — `save()`, queryset `update()`, `bulk_create()`, `bulk_update()` — and fall back to `system` outside a request. `objects_active` returns only active rows.

### 8.3 Log-model strategy (audit trail)

The same triad as the reference services:

```mermaid
classDiagram
    class AddCommonField {
        <<abstract>>
        status
        creation_date / creation_by
        updation_date / updation_by
        save() stamps, secures PII, writes log
    }
    class CommonBooking {
        <<abstract>>
        business columns
    }
    class Booking {
        set_logModel("booking.BookingLog")
    }
    class BookingLog {
        log FK to Booking (related_name logs)
        request_id
        no updation_* columns
    }
    AddCommonField <|-- CommonBooking
    CommonBooking <|-- Booking
    CommonBooking <|-- BookingLog
    Booking "1" --> "*" BookingLog : logs
```

- Every write — `save()`, `update()`, `bulk_create()`, `bulk_update()` — appends a full snapshot to `XLog` in the **same transaction**, with the actor and request id. History is append-only; `log` uses `on_delete=RESTRICT`.
- **Applied to:** OwnerAccount, Property, RoomType, InventoryHold, Booking, Payment, Refund.
- **Not applied to:** RoomNight (a high-churn counter, derivable from holds), Listing (a projection rebuilt from catalog), Amenity (seed data).
- Unique constraints and indexes live on the concrete live model only, so snapshots never collide.

### 8.4 PII encryption at rest

| Data | Table | At rest | Companions |
|---|---|---|---|
| Guest name, email, phone | booking | AES-256-GCM, `ENC_<key-id>:<base64>` | `masked_guest_name`, `masked_guest_email`, `masked_guest_phone` |
| Owner contact email, phone | owner_account | same | `contact_email_hash` (blind index, unique), `masked_contact_email`, `masked_contact_phone` |
| UPI VPA, wallet phone | payment | same | `instrument_display` (masked) |
| Card number, CVV | — | **never stored** (PCI DSS); only brand + last 4 | — |
| Stored idempotent responses | Redis | same cipher | — |

- `EncryptedTextField` encrypts in `get_prep_value` and decrypts in `from_db_value`. Because encryption sits on the field, every write path is covered, log tables included (they reuse the field definitions), and the in-memory instance keeps plaintext.
- A random 96-bit nonce per value; the key id in the prefix allows key rotation.
- The ciphertext is bound to its column with associated data (`<PI type>|<column>`), so it can't be copied into another column. Only the cipher's own `Ciphertext` type skips re-encryption, so user input that looks like ciphertext is still encrypted.
- Equality lookups use a keyed HMAC-SHA256 blind index (`<field>_hash`). The unique index on the owner's contact-email hash blocks duplicate accounts without querying plaintext. Direct lookups on encrypted columns raise an error.
- `masked_<field>` columns serve list screens and owner views without decrypting.
- Commands and DTOs that carry PII or card data hide those fields from `repr`; services log ids only; a logging filter redacts emails, phone numbers and card numbers as a safety net.
- Keys come from env (`PII_ENCRYPTION_KEYS`, `PII_BLIND_INDEX_KEY`), and settings refuse to start with missing or short keys. In production a KMS would supply data keys (envelope encryption).

### 8.5 Redis keyspace

| Purpose | Key | Type / TTL |
|---|---|---|
| Rate limiting | `hb:rl:v1:{scope}:{subject}` | sorted set of request timestamps; TTL = window |
| Idempotency | `hb:idem:v1:{scope}:{principal}:{key}` | encrypted record (state, HMAC fingerprint, response); 60 s in progress, 24 h completed |
| Encrypted-payload replay guard | `hb:enc:v1:{sha256(wrapped key)}` | flag; 5 min |

Redis is never the source of truth:
- If it is down, rate limiting fails open, except for payments, which fail closed.
- Idempotency falls back to Postgres: the payment table's unique key plus a durable replay check in the pay flow, so a payment still cannot be charged twice.
- IDOR ownership is always read from Postgres (no cache), so it can never be stale.

## 9. Concurrency and consistency

1. **Inventory counters.** `room_night(room_type_id, stay_date, total_rooms, reserved_rooms)` has `CHECK (0 <= reserved_rooms AND reserved_rooms <= total_rooms)`. A hold runs `SELECT … WHERE stay_date in [check_in, check_out) ORDER BY stay_date FOR UPDATE`, checks availability, runs `UPDATE … reserved_rooms = reserved_rooms + n`, and inserts the hold — in one transaction.
   - Locking rows in date order means two overlapping stays can't deadlock.
   - The `CHECK` makes overselling impossible even if application code is wrong.
2. **Booking transitions.** Pay, cancel and expire each lock the booking row first (`SELECT … FOR UPDATE`), so a payment racing the expiry job, or a double-clicked cancel, is decided under the lock. "Expired" means `now >= hold_expires_at`, in code and in SQL alike.
3. **Global lock order.** Night rows are always locked ordered by `(room_type_id, stay_date)`, and a transaction never locks the nights of more than one hold. The expiry job therefore handles one booking per transaction, with `FOR UPDATE SKIP LOCKED`, so workers never block each other and never deadlock against new bookings.
4. **Payment idempotency** is layered:
   - Redis `SET NX` rejects duplicates fast.
   - A durable replay check in the pay flow finds an earlier charge by its key before the status check, so a retried success still returns 200 after Redis forgets the key.
   - A unique `idempotency_key` on payment and refund is the final guarantee. The payment row is inserted before the gateway call, so a concurrent duplicate fails on the insert.
   - The gateway receives the same key.
5. **No missing inventory rows.** A hold first inserts any missing night rows (`ON CONFLICT DO NOTHING`), and a daily job extends the horizon, so a free room is never refused because a row wasn't provisioned yet.
6. **Same-transaction events.** The in-process bus runs handlers inside the writer's transaction, so a catalog row, its inventory rows and its discovery listing commit together.
7. **Outcomes that must persist.** A declined payment or an expired hold is committed first; the service returns an outcome and the view answers 402 or 409. Nothing is rolled back by raising after the state change.
8. **Proof.** An integration test races 10 threads for the last room against real Postgres and asserts exactly one booking succeeds. A second test races overlapping stays in both orders to show there is no deadlock.

Alternatives considered:
- **Per-physical-room bookings with an exclusion constraint** (`EXCLUDE USING gist (room_id WITH =, stay WITH &&)`, or PostgreSQL 18 `WITHOUT OVERLAPS`). Rejected: guests book room types, not room numbers (A1).
- **Optimistic version columns.** Clients would retry under contention on popular dates.
- **Redis distributed lock.** Adds a second source of truth for something Postgres already guarantees.

## 10. Security architecture

| Concern | Design |
|---|---|
| Identity | HS256-signed JWT (`iss`, `aud`, `exp`, `nbf`, `sub`, `roles`) verified by middleware: algorithm pinned, all claims required, key ≥ 32 bytes, `sub` format checked. A `TokenVerifier` interface allows RS256/JWKS behind an API gateway later. Dev tokens come from `manage.py mint_token`, plus `/dev/tokens`, which exists only when `ENVIRONMENT=local`. The default environment is `production`, and settings refuse missing, short or committed dev keys outside local. |
| Roles | **GUEST:** search, book, pay, cancel own bookings. **OWNER:** create accounts, onboard and manage own properties, list and view bookings of own properties with guest PII masked. **ADMIN:** everything. |
| IDOR | Views declare `idor_rules` per action: resource, where the id is (path, body or query), and which **relationship** counts (`guest`, `owner`). Access needs the matching role *and* relationship. A user who is both guest and owner can view a stranger's booking at their own property but cannot pay for or cancel it. Ownership is resolved from Postgres through resolvers each app registers. A failed check answers **404**, so existence isn't confirmed. List endpoints are always scoped to the caller. |
| Field-level security | Serializers declare `field_policies`. Write-restricted fields return 403 (e.g. `on_behalf_of_user_id` is ADMIN-only). Read-restricted fields are masked or dropped: guest PII is visible only to the guest themself and ADMIN, and masked for everyone else; `gateway_txn_id` is ADMIN-only. |
| API payload encryption | Hybrid RSA-OAEP-256 (3072-bit) + AES-256-GCM. The client encrypts the body with a fresh AES key and sends that key wrapped by the server's RSA public key (`X-Encrypted-Key`) with a timestamp. Associated data binds method, path, timestamp and direction. Every encryption uses a fresh nonce. A wrapped key is accepted once (Redis replay guard). The response is encrypted with the same AES key. Modes: `off`, `optional` (local default), `enforced`, with an exact-path bypass list (health, docs, schema, public key, dev tokens). |
| Rate limiting | Sliding-window log in Redis, run as one atomic Lua script using Redis server time. Per-scope policies (default 120/min, search 60/min, booking writes 20/min, payments 10/min) keyed by principal, or by client IP when anonymous. The client IP comes from `REMOTE_ADDR`, or from a configured number of trusted proxy hops, so spoofed `X-Forwarded-For` values can't mint fresh buckets. Returns `429` with `Retry-After` and `X-RateLimit-*` headers. Fails open if Redis is down, except payments, which fail closed. Encrypted requests are also IP-limited before any RSA work. |
| PII at rest | §8.4 |
| Audit | Log-model rows record who changed what, when, and in which request. |

## 11. Extensibility

| To add | Change | Untouched |
|---|---|---|
| A search filter | New `SearchFilter` class, decorated with `@register_filter` | `SearchService`, view |
| A payment method | New `PaymentMethodHandler` + enum member | `ChargeService`, booking |
| A pricing strategy | New `PricingStrategy` + `PricingStrategyCode` member | quote engine, discovery, booking |
| A cancellation policy | New `CancellationPolicy` (or a new tier table) + enum member | `CancelBookingService` |
| A booking type | New `BookingTypePolicy` + enum member | booking services |
| A property type | `PropertyType` member, optional `PropertyTypeRule` | onboarding service |
| A real payment gateway | New `PaymentGateway` adapter wired in the container | payment domain and services |
| Different persistence | New repository implementations wired in the container | services, domain |
| A message broker | New `EventPublisher` (outbox + Kafka/RabbitMQ) | publishers, handlers |

## 12. API conventions and errors

- Base path `/hotel/{API_VERSION}/` (default `v1`), JSON only, no trailing slashes. OpenAPI schema at `/hotel/v1/schema`, Swagger UI at `/hotel/v1/docs`.
- Success envelope: `{"status": 2100000, "message": "Success", "data": {...}, "request_id": "..."}`. Error envelope: `{"status": 2105002, "message": "...", "errors": {...}, "data": null, "request_id": "..."}`. Real HTTP status codes are always used.
- Outcome responses keep `data`: a declined payment (402) and an expired hold (409) return the current booking in `data`, because the state change was committed.
- Money is always `{"amount": "9600.00", "currency": "INR"}`; enums are their names (`"PREPAID"`).
- Status codes follow `SS MM EEE`: service `21`, module (`00` common, `01` catalog, `02` inventory, `03` pricing, `04` discovery, `05` booking, `06` payment), error number. Each app keeps its catalogue in `status_code.py`, as in the reference services.
- HTTP mapping: 400 validation · 401 token · 402 payment declined · 403 role or field · 404 not found or not yours · 409 state conflict, no inventory, idempotent request in progress · 422 business rule or idempotency-key reuse · 429 rate limit · 500 unexpected.

## 13. Deployment and configuration

- `docker compose up --build` starts `postgres:18`, `redis:7`, the app (gunicorn) and the scheduler (§6.6). Migrations and demo data (a chain owner with two properties and a standalone owner with one) run on start, so search works immediately.
- Configuration is all environment variables. `.env.example` holds clearly labelled dev-only keys so compose works out of the box; settings refuse those keys unless `ENVIRONMENT=local`. `scripts/bootstrap_env.py` generates fresh local keys.
- `GET /hotel/v1/health` checks Postgres and Redis.

## 14. Path to microservices

1. Each app becomes a service with its own database; there are no cross-app foreign keys to untangle.
2. In-process adapters (`booking/adapters/*`, `discovery/adapters/*`, …) are replaced by HTTP or gRPC clients implementing the same port interfaces. Domain and services don't change.
3. The in-process event bus is replaced by a transactional outbox and a broker. Discovery becomes eventually consistent, with a search-engine read model.
4. Pay and cancel become sagas with compensations: refund if confirmation fails, release on timeout.
5. `hotel_commons` is published to a private package index and versioned. JWTs are verified at the gateway and in each service (RS256/JWKS).

## 15. Trade-offs and future work

- **Gateway call inside the booking transaction.** Simple and atomic with a mock. With a real gateway: create a pending payment, commit, call out, and confirm through a webhook or outbox to keep database transactions short.
- **Search computes availability and price per request.** Fine at this scale. At scale: precomputed availability, cached quotes, a search engine.
- **Synchronous in-process events** trade decoupling for atomicity inside the monolith.
- **Future work:** per-channel rate plans and inventory, partial cancellation, notifications, an outbox, OpenTelemetry tracing (as in the reference service), admin tooling.
