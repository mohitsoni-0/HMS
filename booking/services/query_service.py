"""Read use cases of the booking API (LLD §8.4). Callers are scoped by the view and the IDOR guard."""

from uuid import UUID

from booking.domain.booking import Booking
from booking.interfaces.repositories import BookingRepository


class BookingQueryService:
    def __init__(self, bookings: BookingRepository) -> None:
        self._bookings = bookings

    def get(self, booking_id: UUID) -> Booking:
        return self._bookings.get(booking_id)

    def list_for_guest(
        self, guest_id: str, *, page: int, page_size: int, property_id: UUID | None = None
    ) -> tuple[list[Booking], int]:
        return self._bookings.list_for_guest(guest_id, page=page, page_size=page_size, property_id=property_id)

    def list_for_property(self, property_id: UUID, *, page: int, page_size: int) -> tuple[list[Booking], int]:
        return self._bookings.list_for_property(property_id, page=page, page_size=page_size)
