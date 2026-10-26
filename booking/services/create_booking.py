"""Create a booking: validate, price, hold the rooms and store it (LLD §8.4, HLD §6.3)."""

import logging

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher

from booking.domain.booking import Booking
from booking.domain.booking_types import BookingTypeRegistry
from booking.enums import BookingStatus
from booking.interfaces.ports import CatalogPort, InventoryPort, PricingPort
from booking.interfaces.repositories import BookingRepository
from booking.public.events import BookingCreated
from booking.services.commands import CreateBookingCommand
from booking.services.settings import BookingSettings

logger = logging.getLogger(__name__)


class CreateBookingService:
    def __init__(
        self,
        bookings: BookingRepository,
        catalog: CatalogPort,
        pricing: PricingPort,
        inventory: InventoryPort,
        booking_types: BookingTypeRegistry,
        events: EventPublisher,
        uow: UnitOfWork,
        clock: Clock,
        settings: BookingSettings,
    ) -> None:
        self._bookings = bookings
        self._catalog = catalog
        self._pricing = pricing
        self._inventory = inventory
        self._booking_types = booking_types
        self._events = events
        self._uow = uow
        self._clock = clock
        self._settings = settings

    def execute(self, cmd: CreateBookingCommand) -> Booking:
        stay = DateRange(cmd.check_in, cmd.check_out)
        info = self._catalog.room_type_info(cmd.room_type_id)
        type_policy = self._booking_types.get(cmd.booking_type)
        Booking.validate_request(
            room_type=info,
            stay=stay,
            rooms=cmd.rooms,
            guests=cmd.guests,
            today=self._clock.today(),
            settings=self._settings,
        )
        price = self._pricing.quote(info, stay, cmd.rooms)
        now = self._clock.now()
        booking = Booking.request(
            guest_id=cmd.guest_id,
            room_type=info,
            stay=stay,
            rooms=cmd.rooms,
            guests=cmd.guests,
            type_policy=type_policy,
            contact=cmd.contact,
            price=price,
            now=now,
        )
        with self._uow:
            expires_at = type_policy.hold_expires_at(now, self._settings.hold_ttl)
            hold_id = self._inventory.hold(
                room_type_id=info.room_type_id,
                stay=stay,
                rooms=cmd.rooms,
                reference=str(booking.id),
                expires_at=expires_at,
            )
            booking.attach_hold(hold_id, expires_at)
            if booking.status is BookingStatus.CONFIRMED:  # pay at property: nothing left to wait for
                self._inventory.confirm_hold(hold_id)
            self._bookings.add(booking)
            self._events.publish(
                BookingCreated(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
            )
        logger.info("Booking %s created for room type %s with hold %s", booking.id, booking.room_type_id, hold_id)
        return booking
