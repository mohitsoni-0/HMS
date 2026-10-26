"""Django app config of the payment context."""

from django.apps import AppConfig


class PaymentConfig(AppConfig):
    name = "payment"
    verbose_name = "Payment"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Register the ``payment`` IDOR resolver (LLD §9.8): a payment belongs to its payer."""
        from hotel_commons.security.idor import ownership_registry

        from payment.repositories.payment_repository import PaymentOwnershipResolver

        ownership_registry.register("payment", PaymentOwnershipResolver())
