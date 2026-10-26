"""IDOR ownership of a booking (LLD §8.6), registered as resource "booking" in ``BookingConfig.ready()``.

The guest who made it, and the owner of its property's account. Read straight from Postgres on every
check (no cache), without decrypting the guest's PII. The owner comes from catalog's ``owner_account``
resolver, which reads active accounts only, so deactivating an account revokes its owner's access to the
bookings at once, exactly as it does for the property and the account themselves.
"""

from uuid import UUID

from hotel_commons.security.idor import Ownership, OwnershipRegistry, OwnershipResolver, ownership_registry

from booking.constants import OWNER_ACCOUNT_RESOURCE
from booking.models.booking_models import Booking as BookingModel


class BookingOwnershipResolver(OwnershipResolver):
    def __init__(self, registry: OwnershipRegistry = ownership_registry) -> None:
        self._registry = registry

    def resolve(self, resource_id: UUID) -> Ownership | None:
        row = BookingModel.objects_active.filter(pk=resource_id).values("guest_id", "owner_account_id").first()
        if row is None:
            return None
        account = self._registry.resolve(OWNER_ACCOUNT_RESOURCE, row["owner_account_id"])
        return Ownership(
            guest_user_ids=frozenset({row["guest_id"]}),
            owner_user_ids=account.owner_user_ids if account is not None else frozenset(),
        )
