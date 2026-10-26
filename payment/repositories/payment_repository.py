"""Django payment repository (LLD §2.4, §2.5, §9.3) and the ``payment`` IDOR ownership resolver (§9.8).

``add`` inserts inside its own savepoint and catches ``IntegrityError`` outside it, so a duplicate
idempotency key becomes ``DuplicatePayment`` while the enclosing transaction stays usable. ``save`` loads the
row inside the current transaction and updates only the mutable columns.
"""

import logging
from uuid import UUID

from django.db import IntegrityError, transaction
from hotel_commons.security.idor import Ownership, OwnershipResolver

from payment.domain.errors import DuplicatePayment
from payment.domain.payment import Payment
from payment.interfaces.repositories import PaymentRepository
from payment.models.payment_models import PAYMENT_IDEMPOTENCY_KEY_CONSTRAINT
from payment.models.payment_models import Payment as PaymentRow
from payment.repositories.mappers import PAYMENT_MUTABLE_FIELDS, apply_payment, new_payment_row, payment_from_row
from payment.status_code import PAYMENT_NOT_FOUND

logger = logging.getLogger(__name__)


class DjangoPaymentRepository(PaymentRepository):
    def add(self, payment: Payment) -> None:
        row = new_payment_row(payment)
        try:
            with transaction.atomic():
                row.save(force_insert=True)
        except IntegrityError as exc:
            if _violates(exc, PAYMENT_IDEMPOTENCY_KEY_CONSTRAINT):
                logger.info("Payment %s rejected: idempotency key already used", payment.id)
                raise DuplicatePayment(payment.id) from exc
            raise

    def save(self, payment: Payment) -> None:
        row = PaymentRow.objects.get(pk=payment.id)
        apply_payment(payment, row)
        row.save(update_fields=list(PAYMENT_MUTABLE_FIELDS))

    def get(self, payment_id: UUID) -> Payment:
        return payment_from_row(PaymentRow.objects.get_or_not_found(PAYMENT_NOT_FOUND, pk=payment_id))

    def get_for_update(self, payment_id: UUID) -> Payment:
        row = PaymentRow.objects.select_for_update().get_or_not_found(PAYMENT_NOT_FOUND, pk=payment_id)
        return payment_from_row(row)

    def find_by_idempotency_key(self, key: str) -> Payment | None:
        row = PaymentRow.objects.filter(idempotency_key=key).first()
        return None if row is None else payment_from_row(row)


class PaymentOwnershipResolver(OwnershipResolver):
    """A payment belongs to its payer (the guest who paid); registered as ``payment`` in ``PaymentConfig.ready()``."""

    def resolve(self, resource_id: UUID) -> Ownership | None:
        payer_id = PaymentRow.objects_active.filter(pk=resource_id).values_list("payer_id", flat=True).first()
        return None if payer_id is None else Ownership(guest_user_ids=frozenset({payer_id}))


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """True when ``exc`` was raised by ``constraint``; psycopg reports the name in ``diag.constraint_name``."""
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint or constraint in str(exc)
