"""Catalog routes, mounted at ``hotel/<v>/catalog/`` (LLD §10.1, §10.2 rows 4-9). No trailing slashes."""

from django.urls import path

from catalog.views.amenity_views import AmenityViewSet
from catalog.views.owner_account_views import OwnerAccountViewSet
from catalog.views.property_views import PropertyViewSet, RoomTypeViewSet

urlpatterns = [
    path("amenities", AmenityViewSet.as_view({"get": "list"}), name="catalog-amenities"),
    path("owner-accounts", OwnerAccountViewSet.as_view({"post": "create"}), name="catalog-owner-accounts"),
    path(
        "owner-accounts/<uuid:account_id>/properties",
        PropertyViewSet.as_view({"get": "list_for_account", "post": "onboard"}),
        name="catalog-owner-account-properties",
    ),
    path("properties/<uuid:property_id>", PropertyViewSet.as_view({"get": "retrieve"}), name="catalog-property"),
    path(
        "properties/<uuid:property_id>/room-types",
        RoomTypeViewSet.as_view({"post": "create"}),
        name="catalog-property-room-types",
    ),
]
