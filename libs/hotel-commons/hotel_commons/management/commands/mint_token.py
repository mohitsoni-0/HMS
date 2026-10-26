"""``python manage.py mint_token --sub guest-1 --roles GUEST [--ttl 3600]`` (LLD §3.13).

Prints a bearer token signed with the configured JWT key. Roles are role names, case-insensitive, given
as separate arguments or comma-separated (``--roles GUEST,OWNER``).
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from hotel_commons.security.jwt_tokens import get_token_service
from hotel_commons.security.principal import Role


class Command(BaseCommand):
    help = "Print a signed bearer token for a user id and roles."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--sub", required=True, help="User id, ^[A-Za-z0-9_.@-]{1,64}$")
        parser.add_argument("--roles", required=True, nargs="+", help="GUEST, OWNER and/or ADMIN")
        parser.add_argument("--ttl", type=int, default=None, help="Lifetime in seconds (default JWT_TTL_SECONDS)")

    def handle(self, *args: Any, **options: Any) -> None:
        roles = _parse_roles(options["roles"])
        try:
            token = get_token_service().issue(user_id=options["sub"], roles=roles, ttl_seconds=options["ttl"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(token)


def _parse_roles(values: list[str]) -> list[Role]:
    names = [name.strip().upper() for value in values for name in value.split(",") if name.strip()]
    try:
        return [Role(name) for name in names]
    except ValueError:
        raise CommandError(f"Roles must be among {', '.join(role.value for role in Role)}") from None
