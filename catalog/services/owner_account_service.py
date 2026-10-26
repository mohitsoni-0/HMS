"""Open an owner account (LLD §4.4)."""

import logging

from hotel_commons.domain.unit_of_work import UnitOfWork

from catalog.domain.owner_account import ContactDetails, OwnerAccount
from catalog.interfaces.repositories import OwnerAccountRepository
from catalog.services.commands import OwnerAccountDraft

logger = logging.getLogger(__name__)


class OwnerAccountService:
    def __init__(self, accounts: OwnerAccountRepository, uow: UnitOfWork) -> None:
        self._accounts = accounts
        self._uow = uow

    def open_account(self, owner_user_id: str, draft: OwnerAccountDraft) -> OwnerAccount:
        """A new account owned by ``owner_user_id``; Conflict(OWNER_ACCOUNT_EXISTS) when the contact email is taken."""
        contact = ContactDetails(email=draft.contact_email, phone=draft.contact_phone)
        account = OwnerAccount.open(display_name=draft.display_name, owner_user_id=owner_user_id, contact=contact)
        with self._uow:
            self._accounts.add(account)
        logger.info("Owner account %s opened", account.id)
        return account
