"""Booking routes (LLD §10.1-§10.2), mounted at ``/hotel/<version>/``. No trailing slashes."""

from django.urls import path

from booking.views.booking_views import BookingViewSet

urlpatterns = [
    path("bookings", BookingViewSet.as_view({"get": "list", "post": "create"}), name="booking-list"),
    path("bookings/<uuid:booking_id>", BookingViewSet.as_view({"get": "retrieve"}), name="booking-detail"),
    path("bookings/<uuid:booking_id>/payments", BookingViewSet.as_view({"post": "pay"}), name="booking-pay"),
    path("bookings/<uuid:booking_id>/cancel", BookingViewSet.as_view({"post": "cancel"}), name="booking-cancel"),
]
