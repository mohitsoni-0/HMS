"""``python manage.py seed_demo``: demo owners and properties so search works right after start (LLD §4.4).

- a chain owner (user ``owner-chain``) with a Goa RESORT (WEEKEND pricing, MODERATE policy) and a Bengaluru
  HOTEL (DYNAMIC pricing, FLEXIBLE policy);
- a standalone owner (user ``owner-solo``) with one Goa VILLA (STANDARD pricing, NON_REFUNDABLE).

Everything goes through the catalog services, so inventory is provisioned and the discovery listing projected
exactly as for an API onboarding. Idempotent: an owner whose display name already exists is skipped, and each
owner is seeded in one transaction, so a failed run leaves nothing half-seeded.
"""

from dataclasses import dataclass
from datetime import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from hotel_commons.domain.money import Money

from catalog import container
from catalog.constants import DEFAULT_CHECK_IN_TIME, DEFAULT_CHECK_OUT_TIME
from catalog.domain.address import Address
from catalog.enums import CancellationPolicyCode, PricingStrategyCode, PropertyType
from catalog.models.catalog_models import OwnerAccount
from catalog.services.commands import OwnerAccountDraft, PropertyDraft, RoomTypeDraft


@dataclass(frozen=True, slots=True)
class DemoOwner:
    owner_user_id: str
    account: OwnerAccountDraft
    properties: tuple[PropertyDraft, ...]


def _room_type(name: str, max_occupancy: int, total_rooms: int, rupees: str, *amenities: str) -> RoomTypeDraft:
    return RoomTypeDraft(
        name=name,
        max_occupancy=max_occupancy,
        total_rooms=total_rooms,
        base_price=Money.of_rupees(rupees),
        amenity_codes=frozenset(amenities),
    )


def _property(**fields: Any) -> PropertyDraft:
    defaults = {"check_in_time": DEFAULT_CHECK_IN_TIME, "check_out_time": DEFAULT_CHECK_OUT_TIME}
    return PropertyDraft(**{**defaults, **fields})


SEA_BREEZE_RESORT = _property(
    name="Sea Breeze Resort",
    property_type=PropertyType.RESORT,
    star_rating=4,
    address=Address(line1="Candolim Beach Road", locality="Candolim", city="Goa", state="Goa", postal_code="403515"),
    amenity_codes=frozenset({"WIFI", "POOL", "BREAKFAST", "RESTAURANT", "SPA", "PARKING"}),
    pricing_strategy=PricingStrategyCode.WEEKEND,
    cancellation_policy=CancellationPolicyCode.MODERATE,
    description="Beachfront resort with sea-view rooms.",
    room_types=(
        _room_type("Deluxe Sea View", 2, 10, "4800", "AIR_CONDITIONING", "TV", "BALCONY", "MINIBAR"),
        _room_type("Family Suite", 4, 4, "8500", "AIR_CONDITIONING", "TV", "BALCONY", "KITCHEN"),
    ),
)

GARDEN_CITY_HOTEL = _property(
    name="Garden City Business Hotel",
    property_type=PropertyType.HOTEL,
    star_rating=3,
    address=Address(
        line1="100 Feet Road", locality="Indiranagar", city="Bengaluru", state="Karnataka", postal_code="560038"
    ),
    amenity_codes=frozenset({"WIFI", "GYM", "RESTAURANT", "PARKING", "AIRPORT_SHUTTLE"}),
    pricing_strategy=PricingStrategyCode.DYNAMIC,
    cancellation_policy=CancellationPolicyCode.FLEXIBLE,
    check_in_time=time(12, 0),
    description="Business hotel close to the metro.",
    room_types=(
        _room_type("Standard King", 2, 20, "3200", "AIR_CONDITIONING", "TV"),
        _room_type("Executive Twin", 3, 8, "4500", "AIR_CONDITIONING", "TV", "MINIBAR"),
    ),
)

CASA_ANJUNA_VILLA = _property(
    name="Casa Anjuna Villa",
    property_type=PropertyType.VILLA,
    star_rating=None,
    address=Address(line1="Flea Market Lane", locality="Anjuna", city="Goa", state="Goa", postal_code="403509"),
    amenity_codes=frozenset({"WIFI", "POOL", "PARKING", "PET_FRIENDLY"}),
    pricing_strategy=PricingStrategyCode.STANDARD,
    cancellation_policy=CancellationPolicyCode.NON_REFUNDABLE,
    description="Private four-bedroom villa with a pool.",
    room_types=(_room_type("Entire Villa", 8, 1, "15000", "AIR_CONDITIONING", "KITCHEN", "TV", "BALCONY"),),
)

DEMO_OWNERS = (
    DemoOwner(
        owner_user_id="owner-chain",
        account=OwnerAccountDraft(
            display_name="Coastal & Garden Hotels", contact_email="chain.owner@example.com", contact_phone="9876500001"
        ),
        properties=(SEA_BREEZE_RESORT, GARDEN_CITY_HOTEL),
    ),
    DemoOwner(
        owner_user_id="owner-solo",
        account=OwnerAccountDraft(
            display_name="Anjuna Villa Stays", contact_email="solo.owner@example.com", contact_phone="9876500002"
        ),
        properties=(CASA_ANJUNA_VILLA,),
    ),
)


class Command(BaseCommand):
    help = "Seed demo owner accounts and properties (idempotent)."

    def handle(self, *args: Any, **options: Any) -> None:
        for owner in DEMO_OWNERS:
            if OwnerAccount.objects.filter(display_name=owner.account.display_name).exists():
                self.stdout.write(f"Skipped {owner.account.display_name}: already seeded")
                continue
            self._seed(owner)

    def _seed(self, owner: DemoOwner) -> None:
        with transaction.atomic():
            account = container.owner_account_service().open_account(owner.owner_user_id, owner.account)
            onboarded = [container.onboarding_service().onboard(account.id, draft) for draft in owner.properties]
        summary = f"Seeded {account.display_name} ({owner.owner_user_id}), account {account.id}"
        self.stdout.write(self.style.SUCCESS(summary))
        for property_ in onboarded:
            room_types = ", ".join(f"{room_type.name} x{room_type.total_rooms}" for room_type in property_.room_types)
            self.stdout.write(f"  {property_.property_type.name} {property_.name} [{property_.id}]: {room_types}")
