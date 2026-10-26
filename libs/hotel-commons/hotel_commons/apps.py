"""Django app config of hotel_commons: the start-up security check and OpenAPI extension registration."""

from django.apps import AppConfig
from django.conf import settings

from hotel_commons.settings import validate_security_settings


class HotelCommonsConfig(AppConfig):
    name = "hotel_commons"
    verbose_name = "Hotel Commons"

    def ready(self) -> None:
        # Fail fast on unusable or committed dev keys, judged on the final settings (overrides included).
        validate_security_settings(settings)
        # Importing the module registers PrincipalAuthenticationScheme (BearerAuth) with drf-spectacular.
        import hotel_commons.api.schema  # noqa: F401
