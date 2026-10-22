import asyncio
import sys

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller
from controller.zerodown.config import (
    DEFAULT_MAX_CPU_PERCENT,
    DEFAULT_MAX_ERROR_RATE,
    DEFAULT_MAX_LATENCY_MS,
    DEFAULT_MAX_MEMORY_PERCENT,
    load_config,
    parse_config,
)
from controller.zerodown.state import DeploymentPhase, DeploymentStateStore


def sample_config():
    return {
        "app": {"name": "demo-app", "port": 8000},
        "health": {"endpoint": "/health"},
        "rollout": {"stages": [10, 25, 50, 100]},
        "limits": {
            "max_error_rate": 5,
            "max_latency_ms": 1000,
            "max_cpu_percent": 90,
            "max_memory_percent": 90,
        },
    }


def test_valid_yaml_configuration_loads():
    config = load_config()

    assert config.app.name == "demo-app"
    assert config.app.port == 8000
    assert config.health.endpoint == "/health"
    assert config.rollout.stages == (10, 25, 50, 100)
    assert config.limits.max_error_rate == 5


def test_missing_required_field_has_useful_error():
    raw = sample_config()
    del raw["app"]["name"]

    with pytest.raises(ValueError, match="app is missing required field\\(s\\): name"):
        parse_config(raw)


@pytest.mark.parametrize(
    "stages",
    ([10, 50, 25, 100], [10, 50], [0, 100], [10, 101]),
)
def test_invalid_rollout_stages_are_rejected(stages):
    raw = sample_config()
    raw["rollout"]["stages"] = stages

    with pytest.raises(ValueError, match="rollout.stages"):
        parse_config(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_error_rate", 101),
        ("max_error_rate", -1),
        ("max_latency_ms", 0),
        ("max_cpu_percent", 101),
        ("max_memory_percent", -1),
    ],
)
def test_invalid_safety_thresholds_are_rejected(field, value):
    raw = sample_config()
    raw["limits"][field] = value

    with pytest.raises(ValueError, match=f"limits\\.{field}"):
        parse_config(raw)


def test_optional_settings_receive_defaults():
    config = parse_config({
        "app": {"name": "demo-app", "port": 8000},
        "health": {"endpoint": "/health"},
    })

    assert config.app.build_context == "./app"
    assert config.app.dockerfile == "Dockerfile"
    assert config.health.smoke_endpoints == ()
    assert config.rollout.stages == (10, 25, 50, 100)
    assert config.rollout.stage_seconds == 3
    assert config.rollout.observation_seconds == 15
    assert config.limits.max_error_rate == DEFAULT_MAX_ERROR_RATE
    assert config.limits.max_latency_ms == DEFAULT_MAX_LATENCY_MS
    assert config.limits.max_cpu_percent == DEFAULT_MAX_CPU_PERCENT
    assert config.limits.max_memory_percent == DEFAULT_MAX_MEMORY_PERCENT


def test_invalid_yaml_has_useful_error(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("app: [not: valid", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(path)


def test_state_store_starts_idle_and_updates_phase():
    state_store = DeploymentStateStore()

    assert state_store.snapshot().phase is DeploymentPhase.IDLE

    state_store.set_phase(DeploymentPhase.CANARY_10, "Canary started")

    snapshot = state_store.snapshot()
    assert snapshot.phase is DeploymentPhase.CANARY_10
    assert snapshot.message == "Canary started"
    assert snapshot.updated_at


def test_configuration_api_returns_loaded_yaml_shape():
    with TestClient(controller.app) as client:
        response = client.get("/api/v1/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["app"]["name"] == "demo-app"
    assert payload["health"]["endpoint"] == "/health"
    assert payload["rollout"]["stages"] == [10, 25, 50, 100]
    assert payload["limits"]["max_error_rate"] == 5


def test_deployment_uses_configured_rollout_plan(monkeypatch):
    events = []
    phases = []
    current_config = controller.app.state.zerodown_config
    custom_rollout = replace(
        current_config.rollout,
        stages=(20, 60, 100),
        stage_seconds=0.5,
    )
    monkeypatch.setattr(
        controller.app.state,
        "zerodown_config",
        replace(current_config, rollout=custom_rollout),
    )
    monkeypatch.setattr(
        controller.app.state.deployment_state_store,
        "set_phase",
        lambda phase, message: phases.append((phase, message)),
    )

    async def prepare_candidate():
        controller.record(
            "Built and started test V2",
            "starting",
            DeploymentPhase.STARTING,
        )

    async def validate_v2():
        events.append(("validate", "v2"))
        return True, "healthy"

    def set_traffic(v1, v2):
        events.append(("traffic", v1, v2))

    async def monitor_canary(v2_percent, observation_seconds, simulate_failure=False):
        events.append(("monitor", v2_percent, observation_seconds))
        return True, "healthy"

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "build_and_start_v2", prepare_candidate)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", monitor_canary)

    asyncio.run(controller.deployment_job(observation_seconds=7))

    assert [event for event in events if event[0] == "traffic"] == [
        ("traffic", 100, 0),
        ("traffic", 80, 20),
        ("traffic", 40, 60),
        ("traffic", 0, 100),
    ]
    assert [
        event for event in events
        if event[0] == "monitor"
    ] == [
        ("monitor", 20, 0.5),
        ("monitor", 60, 0.5),
        ("monitor", 100, 0.5),
        ("monitor", 100, 7),
    ]
    assert [phase for phase, _ in phases] == [
        DeploymentPhase.BUILDING,
        DeploymentPhase.STARTING,
        DeploymentPhase.HEALTH_CHECK,
        DeploymentPhase.CANARY,
        DeploymentPhase.CANARY,
        DeploymentPhase.FULL_ROLLOUT,
        DeploymentPhase.SUCCESS,
    ]
    messages = [
        step["message"]
        for step in controller.deployment_state["steps"]
    ]
    assert "Canary stage started: V2=20%" in messages
    assert "Canary stage started: V2=60%" in messages


def test_deployment_status_api_reports_live_rollout_fields(monkeypatch):
    class FakeTrafficRouter:
        @staticmethod
        def get_traffic():
            return {"v1_percent": 75, "v2_percent": 25}

    monkeypatch.setattr(
        controller.app.state,
        "traffic_router",
        FakeTrafficRouter(),
    )
    for key, value in {
        "phase": "CANARY_25",
        "health": "healthy",
        "progress": 50,
        "metrics": {"error_rate": 0.01},
        "rollback": None,
        "candidate_logs": None,
        "message": "Monitoring 25% canary",
    }.items():
        monkeypatch.setitem(controller.deployment_state, key, value)

    with TestClient(controller.app) as client:
        response = client.get("/deployment/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "CANARY_25",
        "version": "v2",
        "traffic": 25,
        "health": "healthy",
        "progress": 50,
        "metrics": {"error_rate": 0.01},
        "rollback": None,
        "candidate_logs": None,
        "message": "Monitoring 25% canary",
        "started_at": controller.deployment_state["started_at"],
        "finished_at": controller.deployment_state["finished_at"],
    }


def test_second_rollout_is_rejected_while_one_is_active(monkeypatch):
    monkeypatch.setitem(controller.deployment_state, "active", True)

    with TestClient(controller.app) as client:
        response = client.post("/deploy", json={"observation_seconds": 1})

    assert response.status_code == 409
    assert response.json()["detail"] == "Deployment already running"


def test_configured_application_port_drives_controller(monkeypatch):
    current_config = controller.app.state.zerodown_config
    monkeypatch.setattr(
        controller.app.state,
        "zerodown_config",
        replace(
            current_config,
            app=replace(current_config.app, port=8123),
        ),
    )
    monkeypatch.setattr(controller, "V2_URL", None)

    assert controller.app_url("v2") == "http://app_v2:8123"


@pytest.mark.parametrize(
    ("metric", "value", "message"),
    [
        ("error_rate", 0.06, "Error rate 6.0%"),
        ("p95_latency_ms", 1001, "p95 latency 1001.0ms"),
        ("cpu_percent", 91, "CPU usage 91.0%"),
        ("memory_percent", 91, "Memory usage 91.0%"),
    ],
)
def test_configured_safety_limits_are_enforced(metric, value, message):
    metrics = {
        "error_rate": 0,
        "p95_latency_ms": 1,
        "cpu_percent": 1,
        "memory_percent": 1,
    }
    metrics[metric] = value

    failure = controller.safety_limit_failure(
        metrics,
        {"probability": 0.1, "risk_percent": 10, "risk_level": "LOW"},
    )

    assert failure is not None
    assert message in failure
