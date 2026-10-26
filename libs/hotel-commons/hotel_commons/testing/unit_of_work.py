"""A ``UnitOfWork`` stand-in for unit tests with in-memory repositories."""

from types import TracebackType


class NoopUnitOfWork:
    """Opens no transaction but counts how the use case used it: blocks entered, committed and rolled back.

    It never swallows an exception, matching ``transaction.atomic()``. Nested blocks count separately, like
    savepoints.
    """

    def __init__(self) -> None:
        self.enters = 0
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "NoopUnitOfWork":
        self.enters += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
