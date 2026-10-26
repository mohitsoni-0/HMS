"""Django app config of the catalog context."""

from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "catalog"
    label = "catalog"
    verbose_name = "Catalog"

    def ready(self) -> None:
        """Register the IDOR ownership resolvers of ``owner_account`` and ``property`` (LLD §4.7)."""
        from hotel_commons.security.idor import ownership_registry

        from catalog.repositories.ownership_resolvers import OwnerAccountOwnershipResolver, PropertyOwnershipResolver

        ownership_registry.register("owner_account", OwnerAccountOwnershipResolver())
        ownership_registry.register("property", PropertyOwnershipResolver())
