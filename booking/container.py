"""Composition root of the booking context: the only place that picks concrete implementations (LLD §2.2).

Services are built once per process. They are stateless, and the Django unit of work keeps its transaction
state on the thread's connection, so one instance serves every thread.
"""

from datetime import timedelta
from functools import cache

from django.conf import settings
from hotel_commons.db.unit_of_work import DjangoUnitOfWork
from hotel_commons.domain.clock import SystemClock
from hotel_commons.events.in_process import event_bus

from booking.adapters.catalog_adapter import CatalogAdapter
from booking.adapters.inventory_adapter import InventoryAdapter
from booking.adapters.payment_adapter import PaymentAdapter
from booking.adapters.pricing_adapter import PricingAdapter
from booking.domain.booking_types import BookingTypeRegistry
from booking.domain.cancellation import CancellationPolicyRegistry
from booking.repositories.booking_repository import DjangoBookingRepository
from booking.services.cancel_booking import CancelBookingService
from booking.services.create_booking import CreateBookingService
from booking.services.expire_bookings import ExpireBookingsService
from booking.services.pay_booking import PayBookingService
from booking.services.query_service import BookingQueryService
from booking.services.settings import BookingSettings


@cache
def booking_settings() -> BookingSettings:
    return BookingSettings(
        hold_ttl=timedelta(seconds=settings.BOOKING_HOLD_TTL_SECONDS),
        max_nights=settings.MAX_STAY_NIGHTS,
        horizon_days=settings.INVENTORY_HORIZON_DAYS,
    )


@cache
def create_booking_service() -> CreateBookingService:
    return CreateBookingService(
        DjangoBookingRepository(),
        CatalogAdapter(),
        PricingAdapter(),
        InventoryAdapter(),
        BookingTypeRegistry(),
        event_bus,
        DjangoUnitOfWork(),
        SystemClock(),
        booking_settings(),
    )


@cache
def pay_booking_service() -> PayBookingService:
    return PayBookingService(
        DjangoBookingRepository(),
        PaymentAdapter(),
        InventoryAdapter(),
        BookingTypeRegistry(),
        event_bus,
        DjangoUnitOfWork(),
        SystemClock(),
    )


@cache
def cancel_booking_service() -> CancelBookingService:
    return CancelBookingService(
        DjangoBookingRepository(),
        InventoryAdapter(),
        PaymentAdapter(),
        CancellationPolicyRegistry(),
        event_bus,
        DjangoUnitOfWork(),
        SystemClock(),
    )


@cache
def expire_bookings_service() -> ExpireBookingsService:
    return ExpireBookingsService(
        DjangoBookingRepository(), InventoryAdapter(), event_bus, DjangoUnitOfWork(), SystemClock()
    )


@cache
def booking_query_service() -> BookingQueryService:
    return BookingQueryService(DjangoBookingRepository())
