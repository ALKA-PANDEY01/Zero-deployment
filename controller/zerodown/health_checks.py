"""Retrying HTTP and Docker-health validation for deployment candidates."""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
import docker


class ValidationStatus(str, Enum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    TIMEOUT = "TIMEOUT"
    SMOKE_TEST_FAILED = "SMOKE_TEST_FAILED"


@dataclass(frozen=True)
class ValidationResult:
    version: str
    status: ValidationStatus
    healthy: bool
    checks: tuple[dict[str, Any], ...]
    message: str
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status.value,
            "healthy": self.healthy,
            "checks": list(self.checks),
            "message": self.message,
            "attempts": self.attempts,
        }


class VersionHealthChecker:
    def __init__(
        self,
        docker_manager: Any,
        config: Any,
        attempts: int = 6,
        retry_delay_seconds: float = 2.0,
        timeout_seconds: float = 2.0,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")

        self.docker_manager = docker_manager
        self.config = config
        self.attempts = attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.timeout_seconds = timeout_seconds

    async def _check_endpoint(
        self,
        base_url: str,
        endpoint: str,
    ) -> tuple[ValidationStatus, dict[str, Any]]:
        for attempt in range(1, self.attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                ) as client:
                    response = await client.get(f"{base_url}{endpoint}")
            except httpx.TimeoutException as error:
                if attempt < self.attempts:
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return (
                    ValidationStatus.TIMEOUT,
                    {
                        "endpoint": endpoint,
                        "status": ValidationStatus.TIMEOUT.value,
                        "attempts": attempt,
                        "message": str(error) or "Request timed out",
                    },
                )
            except httpx.RequestError as error:
                if attempt < self.attempts:
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return (
                    ValidationStatus.UNHEALTHY,
                    {
                        "endpoint": endpoint,
                        "status": ValidationStatus.UNHEALTHY.value,
                        "attempts": attempt,
                        "message": f"Application is unavailable: {error}",
                    },
                )

            if not 200 <= response.status_code < 300:
                if attempt < self.attempts:
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return (
                    ValidationStatus.UNHEALTHY,
                    {
                        "endpoint": endpoint,
                        "status": ValidationStatus.UNHEALTHY.value,
                        "status_code": response.status_code,
                        "attempts": attempt,
                        "message": f"Expected HTTP 2xx, got {response.status_code}",
                    },
                )

            return (
                ValidationStatus.HEALTHY,
                {
                    "endpoint": endpoint,
                    "status": ValidationStatus.HEALTHY.value,
                    "status_code": response.status_code,
                    "attempts": attempt,
                    "message": "Endpoint returned HTTP 2xx",
                },
            )

        raise RuntimeError("Endpoint validation exited without a result")

    async def validate_version(
        self,
        version: str,
        container_name: str | None = None,
        base_url: str | None = None,
    ) -> ValidationResult:
        self.docker_manager.validate_version(version)
        container_name = container_name or self.docker_manager.container_name(
            self.config.app.name,
            version,
        )
        base_url = base_url or self.docker_manager.app_url(
            self.config.app.name,
            version,
            self.config.app.port,
        )
        container = None
        inspect_error = None
        for attempt in range(1, self.attempts + 1):
            try:
                container = await asyncio.to_thread(
                    self.docker_manager.inspect_container,
                    container_name,
                )
                inspect_error = None
            except (
                OSError,
                RuntimeError,
                docker.errors.DockerException,
                ValueError,
            ) as error:
                inspect_error = error

            if container and (
                container.get("status") == "running"
                and container.get("health") == "healthy"
            ):
                break
            if attempt < self.attempts:
                await asyncio.sleep(self.retry_delay_seconds)

        if inspect_error is not None or container is None:
            return ValidationResult(
                version=version,
                status=ValidationStatus.UNHEALTHY,
                healthy=False,
                checks=(),
                message=(
                    f"Could not inspect {version} container: {inspect_error}"
                    if inspect_error
                    else f"{version} container inspection returned no result"
                ),
                attempts=self.attempts,
            )

        container_health = container.get("health", "not_configured")
        if container.get("status") != "running" or container_health != "healthy":
            return ValidationResult(
                version=version,
                status=ValidationStatus.UNHEALTHY,
                healthy=False,
                checks=(
                    {
                        "check": "container_health",
                        "status": ValidationStatus.UNHEALTHY.value,
                        "container_status": container.get("status"),
                        "health": container_health,
                        "message": "Container must be running and not unhealthy",
                        "attempts": attempt,
                    },
                ),
                message=f"{version} container is not healthy",
                attempts=attempt,
            )

        endpoints = [
            ("health", self.config.health.endpoint),
            *(
                ("smoke", endpoint)
                for endpoint in self.config.health.smoke_endpoints
            ),
        ]
        checks = [
            {
                "check": "container_health",
                "status": ValidationStatus.HEALTHY.value,
                "container_status": container.get("status"),
                "health": container_health,
                "message": "Container is running and healthy",
                "attempts": attempt,
            },
        ]
        for check_type, endpoint in endpoints:
            status, check = await self._check_endpoint(
                base_url,
                endpoint,
            )
            checks.append({"check": check_type, **check})
            if status is ValidationStatus.TIMEOUT:
                return ValidationResult(
                    version,
                    status,
                    False,
                    tuple(checks),
                    check["message"],
                    check["attempts"],
                )
            if status is not ValidationStatus.HEALTHY:
                result_status = (
                    ValidationStatus.SMOKE_TEST_FAILED
                    if check_type == "smoke"
                    else ValidationStatus.UNHEALTHY
                )
                return ValidationResult(
                    version,
                    result_status,
                    False,
                    tuple(checks),
                    check["message"],
                    check["attempts"],
                )

        return ValidationResult(
            version=version,
            status=ValidationStatus.HEALTHY,
            healthy=True,
            checks=tuple(checks),
            message=f"{version} passed container health and configured HTTP checks",
            attempts=max(check["attempts"] for check in checks),
        )
