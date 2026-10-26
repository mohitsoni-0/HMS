"""IDOR ownership of a booking (LLD §8.6), registered as resource "booking" in ``BookingConfig.ready()``.

The guest who made it, and the owner of its property's account. Read straight from Postgres on every
check (no cache), without decrypting the guest's PII.
"""

from uuid import UUID

from catalog.public.api import get_catalog_api
from hotel_commons.security.idor import Ownership, OwnershipResolver

from booking.models.booking_models import Booking as BookingModel


class BookingOwnershipResolver(OwnershipResolver):
    def resolve(self, resource_id: UUID) -> Ownership | None:
        row = BookingModel.objects_active.filter(pk=resource_id).values("guest_id", "owner_account_id").first()
        if row is None:
            return None
        owner_user_id = get_catalog_api().owner_user_id(row["owner_account_id"])
        return Ownership(
            guest_user_ids=frozenset({row["guest_id"]}),
            owner_user_ids=frozenset({owner_user_id}) if owner_user_id else frozenset(),
        )
