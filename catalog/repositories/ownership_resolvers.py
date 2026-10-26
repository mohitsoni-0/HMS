"""IDOR ownership resolvers of the catalog resources (LLD §4.7), registered by ``CatalogConfig.ready()``.

Both read active rows only and go straight to Postgres, so a deactivated account or property stops being
reachable at once.
"""

from uuid import UUID

from hotel_commons.models.base import RecordStatus
from hotel_commons.security.idor import Ownership, OwnershipResolver

from catalog.models.catalog_models import OwnerAccount, Property


class OwnerAccountOwnershipResolver(OwnershipResolver):
    """``owner_account`` -> the account's owning user."""

    def resolve(self, resource_id: UUID) -> Ownership | None:
        accounts = OwnerAccount.objects_active.filter(pk=resource_id)
        owner_user_id = accounts.values_list("owner_user_id", flat=True).first()
        return None if owner_user_id is None else Ownership(owner_user_ids=frozenset({owner_user_id}))


class PropertyOwnershipResolver(OwnershipResolver):
    """``property`` -> the owning user of the property's (active) account."""

    def resolve(self, resource_id: UUID) -> Ownership | None:
        owner_user_id = (
            Property.objects_active.filter(pk=resource_id, owner_account__status=RecordStatus.ACTIVE)
            .values_list("owner_account__owner_user_id", flat=True)
            .first()
        )
        return None if owner_user_id is None else Ownership(owner_user_ids=frozenset({owner_user_id}))
