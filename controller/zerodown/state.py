"""Deployment phase model shared by the controller and its REST API."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class DeploymentPhase(str, Enum):
    IDLE = "IDLE"
    BUILDING = "BUILDING"
    STARTING = "STARTING"
    HEALTH_CHECK = "HEALTH_CHECK"
    CANARY_10 = "CANARY_10"
    CANARY_25 = "CANARY_25"
    CANARY_50 = "CANARY_50"
    FULL_ROLLOUT = "FULL_ROLLOUT"
    SUCCESS = "SUCCESS"
    ROLLBACK = "ROLLBACK"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DeploymentSnapshot:
    phase: DeploymentPhase
    message: str
    updated_at: str


class DeploymentStateStore:
    def __init__(self) -> None:
        self._snapshot = DeploymentSnapshot(
            phase=DeploymentPhase.IDLE,
            message="Ready",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def set_phase(self, phase: DeploymentPhase, message: str) -> None:
        self._snapshot = DeploymentSnapshot(
            phase=phase,
            message=message,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def snapshot(self) -> DeploymentSnapshot:
        return self._snapshot
