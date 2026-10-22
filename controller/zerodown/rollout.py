"""Boundary for coordinating the configured progressive rollout."""

from typing import Protocol

from .config import ZeroDownConfig
from .state import DeploymentPhase


class RolloutEngine(Protocol):
    async def deploy(self, config: ZeroDownConfig) -> DeploymentPhase:
        """Run a deployment and return its final phase."""
        ...
