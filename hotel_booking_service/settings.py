"""Django settings for the hotel booking service.

The shared defaults come from ``hotel_commons.settings.base_settings`` (LLD §3.13): DRF, middleware
order, database, Redis, logging and the fail-fast checks on keys and environment. This module adds
only what belongs to this deployable: its six bounded-context apps and its URL and WSGI entry points.
Everything else is configured through environment variables; see ``.env.example``.
"""

from hotel_commons.settings.base_settings import *  # noqa: F401,F403

# A new list rather than ``+=``, so the shared library's module-level list is never mutated.
INSTALLED_APPS = [
    *INSTALLED_APPS,  # noqa: F405
    "django.contrib.postgres",
    "catalog.apps.CatalogConfig",
    "inventory.apps.InventoryConfig",
    "pricing.apps.PricingConfig",
    "discovery.apps.DiscoveryConfig",
    "booking.apps.BookingConfig",
    "payment.apps.PaymentConfig",
]

ROOT_URLCONF = "hotel_booking_service.urls"
WSGI_APPLICATION = "hotel_booking_service.wsgi.application"
