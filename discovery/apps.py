"""Django app config of the discovery context: subscribes the listing projector to catalog events."""

from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    name = "discovery"
    verbose_name = "Discovery"

    def ready(self) -> None:
        from catalog.public.events import PropertyOnboarded, RoomTypeAdded
        from discovery.container import listing_projector
        from hotel_commons.events.in_process import event_bus

        projector = listing_projector()
        event_bus.subscribe(PropertyOnboarded, projector.on_property_onboarded)
        event_bus.subscribe(RoomTypeAdded, projector.on_room_type_added)
