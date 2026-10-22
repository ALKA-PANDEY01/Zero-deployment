import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller
from controller.zerodown.config import HealthConfig
from controller.zerodown.health_checks import (
    ValidationResult,
    ValidationStatus,
    VersionHealthChecker,
)


class FakeDockerManager:
    def __init__(self, container=None, inspect_error=None):
        self.container = container or {
            "name": "zerodown-demo-app-v2",
            "status": "running",
            "health": "healthy",
        }
        self.inspect_error = inspect_error

    @staticmethod
    def validate_version(version):
        if version not in {"v1", "v2"}:
            raise ValueError("version must be either 'v1' or 'v2'")

    @staticmethod
    def container_name(app_name, version):
        return f"zerodown-{app_name}-{version}"

    @staticmethod
    def app_url(app_name, version, port):
        return f"http://zerodown-{app_name}-{version}:{port}"

    def inspect_container(self, name):
        if self.inspect_error:
            raise self.inspect_error
        if self.container is None:
            raise ValueError(f"Container '{name}' was not found")
        return self.container


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def make_checker(
    monkeypatch,
    outcomes,
    smoke_endpoints=(),
    attempts=3,
    health_endpoint="/health",
):
    calls = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def get(self, url):
            calls.append(url)
            sequence = outcomes[url]
            outcome = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(
        "controller.zerodown.health_checks.httpx.AsyncClient",
        FakeAsyncClient,
    )
    config = SimpleNamespace(
        app=SimpleNamespace(
            name="demo-app",
            port=8000,
        ),
        health=HealthConfig(
            endpoint=health_endpoint,
            smoke_endpoints=smoke_endpoints,
        ),
    )
    checker = VersionHealthChecker(
        docker_manager=FakeDockerManager(),
        config=config,
        attempts=attempts,
        retry_delay_seconds=0,
        timeout_seconds=0.1,
    )
    return checker, calls


def healthy_outcomes(smoke_endpoints=()):
    outcomes = {
        "http://zerodown-demo-app-v2:8000/health": [
            FakeResponse({"status": "ok", "version": "v2"}),
        ],
        "http://zerodown-demo-app-v2:8000/version": [
            FakeResponse({"version": "v2"}),
        ],
    }
    for endpoint in smoke_endpoints:
        outcomes[f"http://zerodown-demo-app-v2:8000{endpoint}"] = [
            FakeResponse({"version": "v2"}),
        ]
    return outcomes


def test_configured_health_endpoint_and_smoke_endpoints_are_checked(monkeypatch):
    outcomes = {
        "http://zerodown-demo-app-v2:8000/live": [
            FakeResponse({"version": "v2"}),
        ],
        "http://zerodown-demo-app-v2:8000/version": [
            FakeResponse({"version": "v2"}),
        ],
        "http://zerodown-demo-app-v2:8000/smoke": [
            FakeResponse({"version": "v2"}),
        ],
    }
    checker, calls = make_checker(
        monkeypatch,
        outcomes,
        smoke_endpoints=("/smoke",),
        health_endpoint="/live",
    )

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.HEALTHY
    assert calls == [
        "http://zerodown-demo-app-v2:8000/live",
        "http://zerodown-demo-app-v2:8000/version",
        "http://zerodown-demo-app-v2:8000/smoke",
    ]


def test_healthy_v2_passes_container_health_and_http_smoke_checks(monkeypatch):
    checker, calls = make_checker(monkeypatch, healthy_outcomes(("/ready",)), ("/ready",))

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.HEALTHY
    assert result.healthy
    assert [check["check"] for check in result.checks] == [
        "container_health",
        "health",
        "version",
        "smoke",
    ]
    assert len(calls) == 3


def test_unhealthy_container_fails_before_http_requests(monkeypatch):
    checker, calls = make_checker(
        monkeypatch,
        healthy_outcomes(),
    )
    checker.docker_manager.container["health"] = "unhealthy"

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.UNHEALTHY
    assert not result.healthy
    assert not calls


def test_unavailable_candidate_is_reported_unhealthy(monkeypatch):
    checker, calls = make_checker(monkeypatch, healthy_outcomes())
    checker.docker_manager.container = None

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.UNHEALTHY
    assert "not found" in result.message
    assert not calls


def test_http_timeout_is_retried_and_reported(monkeypatch):
    outcomes = healthy_outcomes()
    outcomes["http://zerodown-demo-app-v2:8000/health"] = [
        httpx.ReadTimeout("timed out"),
    ]
    checker, calls = make_checker(monkeypatch, outcomes, attempts=3)

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.TIMEOUT
    assert not result.healthy
    assert calls.count("http://zerodown-demo-app-v2:8000/health") == 3


def test_http_unavailable_is_retried_and_reported(monkeypatch):
    outcomes = healthy_outcomes()
    outcomes["http://zerodown-demo-app-v2:8000/health"] = [
        httpx.ConnectError("connection refused"),
    ]
    checker, calls = make_checker(monkeypatch, outcomes, attempts=2)

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.UNHEALTHY
    assert "unavailable" in result.message
    assert calls.count("http://zerodown-demo-app-v2:8000/health") == 2


def test_wrong_version_response_is_reported_unhealthy(monkeypatch):
    outcomes = healthy_outcomes()
    outcomes["http://zerodown-demo-app-v2:8000/version"] = [
        FakeResponse({"version": "v1"}),
    ]
    checker, _ = make_checker(monkeypatch, outcomes, attempts=1)

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.UNHEALTHY
    assert "Expected version 'v2', got 'v1'" in result.message


def test_failed_smoke_endpoint_has_distinct_result(monkeypatch):
    outcomes = healthy_outcomes(("/ready",))
    outcomes["http://zerodown-demo-app-v2:8000/ready"] = [
        FakeResponse({"version": "v1"}),
    ]
    checker, _ = make_checker(
        monkeypatch,
        outcomes,
        smoke_endpoints=("/ready",),
        attempts=1,
    )

    result = asyncio.run(checker.validate_version("v2"))

    assert result.status is ValidationStatus.SMOKE_TEST_FAILED


def test_validation_api_returns_structured_result(monkeypatch):
    class FakeChecker:
        async def validate_version(self, version):
            return ValidationResult(
                version=version,
                status=ValidationStatus.HEALTHY,
                healthy=True,
                checks=(),
                message="v2 is healthy",
                attempts=1,
            )

    monkeypatch.setattr(
        controller.app.state,
        "version_health_checker",
        FakeChecker(),
    )

    from fastapi.testclient import TestClient

    with TestClient(controller.app) as client:
        response = client.post("/api/v1/versions/v2/validate")

    assert response.status_code == 200
    assert response.json() == {
        "version": "v2",
        "status": "HEALTHY",
        "healthy": True,
        "checks": [],
        "message": "v2 is healthy",
        "attempts": 1,
    }
