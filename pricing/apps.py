"""Django app config of the pricing context. Pricing has no models, routes or event handlers."""

from django.apps import AppConfig


class PricingConfig(AppConfig):
    name = "pricing"
    verbose_name = "Pricing"
