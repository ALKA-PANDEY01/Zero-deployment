import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller


def reset_deployment_state(monkeypatch):
    monkeypatch.setitem(controller.deployment_state, "status", "idle")
    monkeypatch.setitem(controller.deployment_state, "message", "Ready")
    monkeypatch.setitem(controller.deployment_state, "steps", [])
    monkeypatch.setitem(controller.deployment_state, "started_at", None)
    monkeypatch.setitem(controller.deployment_state, "finished_at", None)


def test_v2_is_checked_before_canary_traffic(monkeypatch):
    reset_deployment_state(monkeypatch)
    events = []

    async def check_endpoint(base_url, path, expected_version=None):
        events.append(("check", path, expected_version))
        return True, f"{path} passed"

    def set_traffic(v1, v2):
        events.append(("traffic", v1, v2))

    async def monitor_canary(v2_percent, observation_seconds, simulate_failure=False):
        events.append(("monitor", v2_percent))
        return True, "healthy"

    monkeypatch.setattr(controller, "check_endpoint", check_endpoint)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", monitor_canary)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert events[:3] == [
        ("check", "/health", "v2"),
        ("check", "/ready", "v2"),
        ("check", "/api/data", "v2"),
    ]
    assert events[3] == ("traffic", 90, 10)
    assert [event for event in events if event[0] == "traffic"] == [
        ("traffic", 90, 10),
        ("traffic", 75, 25),
        ("traffic", 50, 50),
        ("traffic", 0, 100),
    ]
    assert controller.deployment_state["status"] == "succeeded"


def test_unhealthy_v2_triggers_rollback(monkeypatch):
    reset_deployment_state(monkeypatch)
    traffic_changes = []

    async def check_endpoint(base_url, path, expected_version=None):
        return False, "/health returned HTTP 500"

    def set_traffic(v1, v2):
        traffic_changes.append((v1, v2))

    monkeypatch.setattr(controller, "check_endpoint", check_endpoint)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert traffic_changes == [(100, 0)]
    assert controller.deployment_state["status"] == "rolled_back"
    messages = [
        step["message"]
        for step in controller.deployment_state["steps"]
    ]
    assert any("V2 validation failed" in message for message in messages)
    assert "Rollback complete: V1 receives 100% traffic" in messages


def test_v2_health_failure_during_canary_rolls_back(monkeypatch):
    reset_deployment_state(monkeypatch)
    traffic_changes = []

    async def validate_v2():
        return True, "V2 healthy"

    def set_traffic(v1, v2):
        traffic_changes.append((v1, v2))

    async def monitor_canary(v2_percent, observation_seconds, simulate_failure=False):
        return False, "V2 /health returned HTTP 500"

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", monitor_canary)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert traffic_changes == [(90, 10), (100, 0)]
    assert controller.deployment_state["status"] == "rolled_back"
    messages = [
        step["message"]
        for step in controller.deployment_state["steps"]
    ]
    assert "Canary stage failed: V2 /health returned HTTP 500" in messages
    assert "Rollback complete: V1 receives 100% traffic" in messages
