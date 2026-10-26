"""Django models of the booking context; ``booking_models`` holds the Booking / BookingLog triad."""

from booking.models.booking_models import Booking, BookingLog

__all__ = ["Booking", "BookingLog"]
