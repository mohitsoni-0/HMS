"""Root URL configuration (LLD §10.1): every context's routes are mounted here once, under ``/hotel/<API_VERSION>/``.

Inventory and pricing have no HTTP endpoints. Other contexts call them through their public APIs.
"""

from django.conf import settings
from django.urls import include, path

API = f"hotel/{settings.API_VERSION}/"

urlpatterns = [
    path(API, include("hotel_commons.api.urls")),  # health, security/public-key, dev/tokens (local), schema, docs
    path(API + "catalog/", include("catalog.urls")),
    path(API + "discovery/", include("discovery.urls")),
    path(API, include("booking.urls")),  # bookings, bookings/<uuid:booking_id>[/payments|/cancel]
    path(API, include("payment.urls")),  # payments/<uuid:payment_id>
]

handler404 = "hotel_commons.api.error_views.not_found"
handler500 = "hotel_commons.api.error_views.server_error"
