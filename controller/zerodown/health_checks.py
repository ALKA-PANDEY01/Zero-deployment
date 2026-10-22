"""Boundary for application health and smoke checks."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HealthCheckResult:
    healthy: bool
    status_code: int | None
    message: str


class HealthCheckService(Protocol):
    async def check(self, base_url: str, endpoint: str) -> HealthCheckResult:
        """Check a configured application endpoint."""
        ...
