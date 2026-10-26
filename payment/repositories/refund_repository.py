"""Django refund repository (LLD §2.4, §9.3).

Refunds are only added, while their payment row is locked by ``RefundService``, which re-checks the
idempotency key under that lock; the unique key on ``payment.refund`` is the last line of defence.
"""

from payment.domain.refund import Refund
from payment.interfaces.repositories import RefundRepository
from payment.models.payment_models import Refund as RefundRow
from payment.repositories.mappers import new_refund_row, refund_from_row


class DjangoRefundRepository(RefundRepository):
    def add(self, refund: Refund) -> None:
        new_refund_row(refund).save(force_insert=True)

    def find_by_idempotency_key(self, key: str) -> Refund | None:
        row = RefundRow.objects.filter(idempotency_key=key).first()
        return None if row is None else refund_from_row(row)
