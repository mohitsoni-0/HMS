"""How many rooms a room type sells per night (LLD §5.2): the template every provisioned night starts from."""

from dataclasses import dataclass
from uuid import UUID

from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED


@dataclass(slots=True)
class Allotment:
    room_type_id: UUID
    total_rooms: int

    def __post_init__(self) -> None:
        if type(self.total_rooms) is not int or self.total_rooms < 0:
            raise ValidationFailed(VALIDATION_FAILED, errors={"total_rooms": "must be a whole number, 0 or more"})
