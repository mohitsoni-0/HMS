"""Create a booking: validate, price, hold the rooms and store it (LLD §8.4, HLD §6.3).

A create sent with an Idempotency-Key is idempotent in Postgres, not only in Redis: the key is stored on the
booking under a unique (guest, key), so a retry returns the first booking even when Redis was down or has
forgotten the key. A concurrent retry that loses the race (to the insert, or to the last rooms) rolls back
its own work and returns the winner's booking.
"""

import logging
from dataclasses import dataclass

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher
from hotel_commons.exceptions import BusinessRuleViolation, Conflict
from hotel_commons.status_code import IDEMPOTENCY_KEY_REUSED

from booking.domain.booking import Booking
from booking.domain.booking_types import BookingTypeRegistry
from booking.interfaces.ports import CatalogPort, InventoryPort, PricingPort
from booking.interfaces.repositories import BookingRepository, DuplicateIdempotencyKey
from booking.public.events import BookingCreated
from booking.services.commands import CreateBookingCommand
from booking.services.settings import BookingSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreateBookingResult:
    booking: Booking
    replayed: bool  # True when an earlier request with the same Idempotency-Key made the booking


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

    def execute(self, cmd: CreateBookingCommand) -> CreateBookingResult:
        prior = self._prior_booking(cmd)
        if prior is not None:
            return CreateBookingResult(prior, replayed=True)
        try:
            return CreateBookingResult(self._create(cmd), replayed=False)
        except (DuplicateIdempotencyKey, Conflict):  # rolled back; a concurrent retry may have won the key
            prior = self._prior_booking(cmd)
            if prior is None:
                raise
            return CreateBookingResult(prior, replayed=True)

    def _prior_booking(self, cmd: CreateBookingCommand) -> Booking | None:
        """The guest's booking made under the command's key; IDEMPOTENCY_KEY_REUSED when it was made for a
        different request, as the Redis layer answers within its TTL."""
        if cmd.idempotency_key is None:
            return None
        prior = self._bookings.find_by_idempotency_key(cmd.guest_id, cmd.idempotency_key)
        if prior is None:
            return None
        if not _same_request(prior, cmd):
            raise BusinessRuleViolation(IDEMPOTENCY_KEY_REUSED)
        logger.info("Booking %s replayed for its idempotency key", prior.id)
        return prior

    def _create(self, cmd: CreateBookingCommand) -> Booking:
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
            idempotency_key=cmd.idempotency_key,
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
            if type_policy.confirms_hold_on_create:  # e.g. pay at property: nothing left to wait for
                self._inventory.confirm_hold(hold_id)
            self._bookings.add(booking)
            self._events.publish(
                BookingCreated(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
            )
        logger.info("Booking %s created for room type %s with hold %s", booking.id, booking.room_type_id, hold_id)
        return booking


def _same_request(booking: Booking, cmd: CreateBookingCommand) -> bool:
    return (
        booking.room_type_id == cmd.room_type_id
        and (booking.stay.start, booking.stay.end) == (cmd.check_in, cmd.check_out)
        and (booking.rooms, booking.guests) == (cmd.rooms, cmd.guests)
        and booking.booking_type is cmd.booking_type
        and booking.contact == cmd.contact
    )
