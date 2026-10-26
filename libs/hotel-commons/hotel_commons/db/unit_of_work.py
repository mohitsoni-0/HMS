"""Django implementation of the ``UnitOfWork`` port (LLD §2.3, §3.4).

The outermost ``with uow:`` runs a transaction and every nested one a savepoint, so a use case called from
another use case commits or rolls back with it. Container-built services share one instance across threads;
that is safe because Django keeps the transaction state of an ``Atomic`` on the thread-local connection,
which is also what makes one ``@transaction.atomic`` reusable by every call.
"""

from types import TracebackType

from django.db import transaction


class DjangoUnitOfWork:
    """``transaction.atomic(using=alias)`` as a re-entrant, thread-safe unit of work."""

    def __init__(self, using: str = "default") -> None:
        self._atomic = transaction.atomic(using=using)

    def __enter__(self) -> "DjangoUnitOfWork":
        self._atomic.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._atomic.__exit__(exc_type, exc, traceback)
