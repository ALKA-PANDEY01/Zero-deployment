"""Boundary for changing traffic between stable and candidate applications."""

from typing import Protocol


class TrafficRouter(Protocol):
    def set_weights(
        self,
        stable_target: str,
        candidate_target: str,
        stable_percent: int,
        candidate_percent: int,
    ) -> None:
        """Apply traffic percentages to the stable and candidate targets."""
        ...
