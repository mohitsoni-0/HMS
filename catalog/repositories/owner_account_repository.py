"""Django ``OwnerAccountRepository`` (LLD §4.3, §2.4, §2.5)."""

import logging
from uuid import UUID

from django.db import IntegrityError, transaction
from hotel_commons.exceptions import Conflict

from catalog.domain.owner_account import OwnerAccount as OwnerAccountEntity
from catalog.interfaces.repositories import OwnerAccountRepository
from catalog.models.catalog_models import OwnerAccount
from catalog.repositories.mappers import owner_account_to_domain, owner_account_to_model
from catalog.status_code import OWNER_ACCOUNT_EXISTS, OWNER_ACCOUNT_NOT_FOUND

logger = logging.getLogger(__name__)


class DjangoOwnerAccountRepository(OwnerAccountRepository):
    def add(self, account: OwnerAccountEntity) -> None:
        row = owner_account_to_model(account)
        try:
            with transaction.atomic():  # savepoint: a duplicate leaves the caller's transaction usable
                row.save(force_insert=True)
        except IntegrityError:
            # The blind index on the contact email is the table's only unique key besides the UUIDv7 id.
            logger.info("Owner account %s rejected: contact email already registered", account.id)
            raise Conflict(OWNER_ACCOUNT_EXISTS) from None
        account.created_at = row.creation_date

    def get(self, account_id: UUID) -> OwnerAccountEntity:
        return owner_account_to_domain(OwnerAccount.objects.get_or_not_found(OWNER_ACCOUNT_NOT_FOUND, pk=account_id))
