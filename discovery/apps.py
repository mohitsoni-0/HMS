"""Django app config of the discovery context: subscribes the catalog event adapter to catalog events."""

from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    name = "discovery"
    verbose_name = "Discovery"

    def ready(self) -> None:
        from catalog.public.events import PropertyOnboarded, RoomTypeAdded
        from discovery.container import catalog_event_subscriber
        from hotel_commons.events.in_process import event_bus

        subscriber = catalog_event_subscriber()
        event_bus.subscribe(PropertyOnboarded, subscriber.on_property_onboarded)
        event_bus.subscribe(RoomTypeAdded, subscriber.on_room_type_added)
