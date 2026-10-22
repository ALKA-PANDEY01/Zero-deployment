"""Boundary for returning traffic to the previous stable application."""

from typing import Protocol


class RollbackEngine(Protocol):
    async def rollback(self) -> None:
        """Restore traffic to the previous stable version."""
        ...
