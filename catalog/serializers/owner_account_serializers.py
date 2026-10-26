"""Owner-account serializers (LLD §10.3)."""

from hotel_commons.security.pi_type import PiType
from hotel_commons.security.principal import Role
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import (
    HotelCharField,
    HotelDateTimeField,
    HotelEmailField,
    HotelEncryptedCharField,
    HotelPhoneField,
    HotelUUIDField,
)

from catalog.constants import OWNER_DISPLAY_NAME_MAX_LENGTH, OWNER_DISPLAY_NAME_MIN_LENGTH
from catalog.services.commands import OwnerAccountDraft


class OwnerAccountCreateSerializer(BaseHotelSerializer):
    display_name = HotelCharField(min_length=OWNER_DISPLAY_NAME_MIN_LENGTH, max_length=OWNER_DISPLAY_NAME_MAX_LENGTH)
    contact_email = HotelEmailField()
    contact_phone = HotelPhoneField()

    def to_draft(self) -> OwnerAccountDraft:
        data = self.validated_data
        return OwnerAccountDraft(
            display_name=data["display_name"], contact_email=data["contact_email"], contact_phone=data["contact_phone"]
        )


class OwnerAccountSerializer(BaseHotelSerializer):
    """An ``OwnerAccount``. Contact details are plain for ADMIN and the account's owner, masked for anyone else."""

    account_id = HotelUUIDField(source="id")
    display_name = HotelCharField()
    owner_user_id = HotelCharField()
    contact_email = HotelEncryptedCharField(
        source="contact.email", read_roles=[Role.ADMIN], subject_attr="owner_user_id", mask_as=PiType.EMAIL
    )
    contact_phone = HotelEncryptedCharField(
        source="contact.phone", read_roles=[Role.ADMIN], subject_attr="owner_user_id", mask_as=PiType.PHONE
    )
    created_at = HotelDateTimeField(allow_null=True)
