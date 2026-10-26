"""Django ``ListingRepository`` (LLD §7.2): storage-agnostic predicates become ``Q`` objects.

``upsert_many`` is one ``INSERT ... ON CONFLICT (room_type_id) DO UPDATE``, so re-projecting a property (or
two transactions projecting the same room type) never fails on the primary key. The listing is a projection
without a log, so the bulk path loses no audit trail; rows that already exist get ``updation_*`` stamped.
"""

from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from django.db.models import Q

from discovery.domain.filters import Op, Predicate
from discovery.domain.listing import Listing
from discovery.interfaces.repositories import ListingRepository
from discovery.models.listing_models import Listing as ListingRow
from hotel_commons.domain.money import Money
from hotel_commons.models.base import audit_values

_LOOKUPS: Final[Mapping[Op, str]] = {
    Op.EQ: "exact",
    Op.IEQ: "iexact",
    Op.IN: "in",
    Op.GTE: "gte",
    Op.LTE: "lte",
    Op.CONTAINS_ALL: "contains",  # PostgreSQL array @> array, served by the GIN index
}
_COLLECTION_OPS: Final = frozenset({Op.IN, Op.CONTAINS_ALL})
_PROJECTED_FIELDS: Final = (
    "property_id",
    "property_name",
    "property_type",
    "star_rating",
    "city",
    "city_key",
    "locality",
    "amenity_codes",
    "room_type_name",
    "max_occupancy",
    "base_price_paisa",
    "currency",
    "pricing_strategy",
    "is_active",
)


class DjangoListingRepository(ListingRepository):
    def upsert_many(self, listings: Sequence[Listing]) -> None:
        if not listings:
            return
        rows = [_to_row(listing) for listing in listings]
        existing = set(ListingRow.objects.filter(pk__in=[row.pk for row in rows]).values_list("pk", flat=True))
        update_stamps = audit_values(ListingRow, adding=False)
        for row in rows:
            if row.pk in existing:
                for name, value in update_stamps.items():
                    setattr(row, name, value)
        ListingRow.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["room_type_id"],
            update_fields=[*_PROJECTED_FIELDS, *update_stamps],
        )

    def find(
        self, predicates: Sequence[Predicate], limit: int, after: tuple[UUID, UUID] | None = None
    ) -> list[Listing]:
        condition = Q()
        for predicate in predicates:
            condition &= _to_q(predicate)
        if after is not None:
            property_id, room_type_id = after
            condition &= Q(property_id__gt=property_id) | Q(property_id=property_id, room_type_id__gt=room_type_id)
        rows = ListingRow.objects.filter(condition).order_by("property_id", "room_type_id")[:limit]
        return [_to_domain(row) for row in rows]


def _to_q(predicate: Predicate) -> Q:
    value = list(predicate.value) if predicate.op in _COLLECTION_OPS else predicate.value
    return Q(**{f"{predicate.field}__{_LOOKUPS[predicate.op]}": value})


def _to_row(listing: Listing) -> ListingRow:
    return ListingRow(
        room_type_id=listing.room_type_id,
        property_id=listing.property_id,
        property_name=listing.property_name,
        property_type=listing.property_type,
        star_rating=listing.star_rating,
        city=listing.city,
        city_key=listing.city_key,
        locality=listing.locality,
        amenity_codes=sorted(listing.amenity_codes),
        room_type_name=listing.room_type_name,
        max_occupancy=listing.max_occupancy,
        base_price_paisa=listing.base_price.paisa,
        currency=listing.base_price.currency,
        pricing_strategy=listing.pricing_strategy,
        is_active=listing.is_active,
    )


def _to_domain(row: ListingRow) -> Listing:
    return Listing(
        room_type_id=row.room_type_id,
        property_id=row.property_id,
        property_name=row.property_name,
        property_type=row.property_type,
        star_rating=row.star_rating,
        city=row.city,
        city_key=row.city_key,
        locality=row.locality,
        amenity_codes=frozenset(row.amenity_codes),
        room_type_name=row.room_type_name,
        max_occupancy=row.max_occupancy,
        base_price=Money(row.base_price_paisa, row.currency),
        pricing_strategy=row.pricing_strategy,
        is_active=row.is_active,
    )
