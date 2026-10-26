"""Django app config of the booking context."""

from django.apps import AppConfig

from booking.constants import BOOKING_RESOURCE


class BookingConfig(AppConfig):
    name = "booking"
    label = "booking"
    verbose_name = "Booking"

    def ready(self) -> None:
        """Register the "booking" IDOR resolver: the booking's guest, and its property's owner (LLD §8.6)."""
        from hotel_commons.security.idor import ownership_registry

        from booking.repositories.ownership import BookingOwnershipResolver

        ownership_registry.register(BOOKING_RESOURCE, BookingOwnershipResolver())
