"""Unit-of-work port (LLD §2.3, §3.4).

A use case runs inside ``with uow:``. Leaving the block normally commits; an exception rolls back and
propagates. ``hotel_commons.db.unit_of_work.DjangoUnitOfWork`` maps this to ``transaction.atomic()`` (nested
blocks become savepoints) and ``hotel_commons.testing.unit_of_work.NoopUnitOfWork`` stands in for unit tests.
"""

from typing import Protocol


class UnitOfWork(Protocol):
    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, *exc: object) -> bool | None: ...
