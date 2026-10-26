"""Composition root of the payment context: the only place that picks implementations (LLD §2.2).

Everything is built once per process. The mock gateway must be a singleton, because its idempotency memory
lives in the instance; the services are stateless and safe to share across threads.
"""

from functools import lru_cache

from hotel_commons.db.unit_of_work import DjangoUnitOfWork
from hotel_commons.domain.clock import SystemClock

from payment.adapters.mock_gateway import MockPaymentGateway
from payment.domain.methods import CardHandler, PaymentMethodRegistry, UpiHandler, WalletHandler
from payment.interfaces.gateway import PaymentGateway
from payment.repositories.payment_repository import DjangoPaymentRepository
from payment.repositories.refund_repository import DjangoRefundRepository
from payment.services.charge_service import ChargeService
from payment.services.query_service import PaymentQueryService
from payment.services.refund_service import RefundService


@lru_cache(maxsize=1)
def payment_gateway() -> PaymentGateway:
    return MockPaymentGateway()


@lru_cache(maxsize=1)
def payment_method_registry() -> PaymentMethodRegistry:
    registry = PaymentMethodRegistry()
    for handler in (CardHandler(SystemClock()), UpiHandler(), WalletHandler()):
        registry.register(handler)
    return registry


@lru_cache(maxsize=1)
def charge_service() -> ChargeService:
    return ChargeService(DjangoPaymentRepository(), payment_method_registry(), payment_gateway(), DjangoUnitOfWork())


@lru_cache(maxsize=1)
def refund_service() -> RefundService:
    return RefundService(DjangoPaymentRepository(), DjangoRefundRepository(), payment_gateway(), DjangoUnitOfWork())


@lru_cache(maxsize=1)
def payment_query_service() -> PaymentQueryService:
    return PaymentQueryService(DjangoPaymentRepository())
