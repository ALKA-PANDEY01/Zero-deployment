"""Boundary for collecting rollout health and resource metrics."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeploymentMetrics:
    error_rate: float | None = None
    latency_ms: float | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None


class MetricsCollector(Protocol):
    async def collect(self, target: str) -> DeploymentMetrics:
        """Collect the configured safety signals for a target."""
        ...
