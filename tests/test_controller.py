import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller
from controller.risk_model import predict_deployment_risk


def reset_deployment_state(monkeypatch):
    monkeypatch.setitem(controller.deployment_state, "status", "idle")
    monkeypatch.setitem(controller.deployment_state, "phase", "IDLE")
    monkeypatch.setitem(controller.deployment_state, "message", "Ready")
    monkeypatch.setitem(controller.deployment_state, "steps", [])
    monkeypatch.setitem(controller.deployment_state, "started_at", None)
    monkeypatch.setitem(controller.deployment_state, "finished_at", None)
    monkeypatch.setitem(controller.deployment_state, "active", False)
    monkeypatch.setitem(controller.deployment_state, "progress", 0)
    monkeypatch.setitem(controller.deployment_state, "traffic", 0)
    monkeypatch.setitem(controller.deployment_state, "health", "unknown")
    monkeypatch.setitem(controller.deployment_state, "metrics", {})
    monkeypatch.setitem(controller.deployment_state, "rollback", None)
    monkeypatch.setitem(controller.deployment_state, "candidate_logs", None)
    monkeypatch.setitem(controller.deployment_state, "container_name", None)


def mock_candidate_start(monkeypatch):
    async def prepare():
        controller.record(
            "Built and started test V2",
            "starting",
            controller.DeploymentPhase.STARTING,
        )

    monkeypatch.setattr(controller, "build_and_start_v2", prepare)


def test_v2_is_checked_before_canary_traffic(monkeypatch):
    reset_deployment_state(monkeypatch)
    mock_candidate_start(monkeypatch)
    events = []

    async def validate_v2():
        events.append(("validate", "v2"))
        return True, "V2 passed validation"

    def set_traffic(v1, v2):
        events.append(("traffic", v1, v2))

    async def monitor_canary(v2_percent, observation_seconds, simulate_failure=False):
        events.append(("monitor", v2_percent))
        return True, "healthy"

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", monitor_canary)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert events[:2] == [
        ("traffic", 100, 0),
        ("validate", "v2"),
    ]
    assert events.index(("validate", "v2")) < events.index(("traffic", 90, 10))
    assert [event for event in events if event[0] == "traffic"] == [
        ("traffic", 100, 0),
        ("traffic", 90, 10),
        ("traffic", 75, 25),
        ("traffic", 50, 50),
        ("traffic", 0, 100),
    ]
    assert controller.deployment_state["status"] == "succeeded"
    assert (
        controller.app.state.deployment_state_store.snapshot().phase
        is controller.DeploymentPhase.SUCCESS
    )


def test_invalid_runtime_stage_fails_before_building(monkeypatch):
    reset_deployment_state(monkeypatch)
    current = controller.app.state.zerodown_config
    monkeypatch.setattr(
        controller.app.state,
        "zerodown_config",
        type(current)(
            app=current.app,
            health=current.health,
            rollout=type(current.rollout)(
                stages=(10, 120, 100),
                stage_seconds=1,
                observation_seconds=1,
            ),
            limits=current.limits,
        ),
    )
    traffic_changes = []
    built = []
    monkeypatch.setattr(
        controller,
        "set_traffic",
        lambda v1, v2: traffic_changes.append((v1, v2)),
    )

    async def prepare():
        built.append(True)

    monkeypatch.setattr(controller, "build_and_start_v2", prepare)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert not built
    assert traffic_changes == [(100, 0)]
    assert controller.deployment_state["status"] == "failed"
    assert controller.deployment_state["phase"] == "FAILED"


def test_interrupted_rollout_restores_v1_traffic(monkeypatch):
    reset_deployment_state(monkeypatch)
    mock_candidate_start(monkeypatch)
    traffic_changes = []
    stage_started = asyncio.Event()

    async def validate_v2():
        return True, "healthy"

    def set_traffic(v1, v2):
        traffic_changes.append((v1, v2))
        controller.deployment_state["traffic"] = v2

    async def wait_for_interrupt(v2_percent, observation_seconds, simulate_failure=False):
        stage_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", wait_for_interrupt)

    async def run_interrupted_job():
        task = asyncio.create_task(controller.deployment_job(1))
        await stage_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_interrupted_job())

    assert traffic_changes[-1] == (100, 0)
    assert controller.deployment_state["status"] == "interrupted"
    assert controller.deployment_state["phase"] == "ROLLBACK"
    assert controller.deployment_state["active"] is False


def test_unhealthy_v2_triggers_rollback(monkeypatch):
    reset_deployment_state(monkeypatch)
    mock_candidate_start(monkeypatch)
    traffic_changes = []

    async def validate_v2():
        return False, "V2 health check returned HTTP 500"

    def set_traffic(v1, v2):
        traffic_changes.append((v1, v2))

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)

    asyncio.run(controller.deployment_job(observation_seconds=1))

    assert traffic_changes == [(100, 0), (100, 0)]
    assert controller.deployment_state["status"] == "rolled_back"
    assert (
        controller.app.state.deployment_state_store.snapshot().phase
        is controller.DeploymentPhase.ROLLBACK
    )
    messages = [
        step["message"]
        for step in controller.deployment_state["steps"]
    ]
    assert "V2 validation failed: V2 health check returned HTTP 500" in messages
    assert "Rollback complete: V1 receives 100% traffic" in messages


def test_v2_health_failure_during_canary_rolls_back(monkeypatch):
    reset_deployment_state(monkeypatch)
    mock_candidate_start(monkeypatch)
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

    assert traffic_changes == [(100, 0), (90, 10), (100, 0)]
    assert controller.deployment_state["status"] == "rolled_back"
    messages = [
        step["message"]
        for step in controller.deployment_state["steps"]
    ]
    assert "Canary stage failed: V2 /health returned HTTP 500" in messages
    assert "Rollback complete: V1 receives 100% traffic" in messages
    assert controller.deployment_state["rollback"] == {
        "status": "ROLLBACK",
        "reason": "health_check_failed",
        "detail": "V2 /health returned HTTP 500",
        "failed_version": "v2",
        "traffic_at_failure": 10,
        "stable_version": "v1",
    }


def test_demo_failure_rolls_back_at_second_configured_stage(monkeypatch):
    reset_deployment_state(monkeypatch)
    mock_candidate_start(monkeypatch)
    current = controller.app.state.zerodown_config
    monkeypatch.setattr(
        controller.app.state,
        "zerodown_config",
        type(current)(
            app=current.app,
            health=current.health,
            rollout=type(current.rollout)(
                stages=(10, 25, 50, 100),
                stage_seconds=0.1,
                observation_seconds=1,
            ),
            limits=current.limits,
        ),
    )
    events = []

    async def validate_v2():
        return True, "V2 healthy"

    async def inject_failure(enabled):
        events.append(("inject", enabled))

    def set_traffic(v1, v2):
        events.append(("traffic", v1, v2))
        controller.deployment_state["traffic"] = v2

    async def monitor_canary(v2_percent, observation_seconds, simulate_failure=False):
        events.append(("monitor", v2_percent))
        if v2_percent == 25:
            controller.deployment_state["health"] = "unhealthy"
            controller.deployment_state["metrics"] = {
                "error_rate": 1.0,
                "health_ok": False,
            }
            return False, "V2 /health returned HTTP 500"
        return True, "healthy"

    monkeypatch.setattr(controller, "validate_v2", validate_v2)
    monkeypatch.setattr(controller, "set_v2_demo_failure", inject_failure)
    monkeypatch.setattr(controller, "set_traffic", set_traffic)
    monkeypatch.setattr(controller, "monitor_canary", monitor_canary)

    asyncio.run(controller.deployment_job(1, simulate_failure=True))

    assert ("traffic", 90, 10) in events
    assert ("traffic", 75, 25) in events
    assert ("traffic", 50, 50) not in events
    assert ("traffic", 0, 100) not in events
    assert events[-1] == ("traffic", 100, 0)
    assert controller.deployment_state["phase"] == "ROLLBACK"
    assert controller.deployment_state["health"] == "unhealthy"
    assert controller.deployment_state["rollback"]["traffic_at_failure"] == 25


def test_set_traffic_delegates_to_router(monkeypatch):
    calls = []

    class FakeTrafficRouter:
        def set_percentages(self, v1, v2):
            calls.append((v1, v2))

    monkeypatch.setattr(
        controller.app.state,
        "traffic_router",
        FakeTrafficRouter(),
    )

    controller.set_traffic(50, 50)

    assert calls == [(50, 50)]


def test_risk_model_distinguishes_healthy_and_unhealthy_metrics():
    healthy = predict_deployment_risk({
        "cpu_percent": 12,
        "memory_percent": 30,
        "p95_latency_ms": 45,
        "error_rate": 0.0,
        "http_5xx_rate": 0.0,
        "success_rate": 1.0,
        "health_ok": True,
    })
    unhealthy = predict_deployment_risk({
        "cpu_percent": 95,
        "memory_percent": 93,
        "p95_latency_ms": 900,
        "error_rate": 0.2,
        "http_5xx_rate": 0.15,
        "success_rate": 0.8,
        "health_ok": False,
    })
    health_failure = predict_deployment_risk({
        "cpu_percent": 20,
        "memory_percent": 30,
        "p95_latency_ms": 40,
        "error_rate": 0.0,
        "http_5xx_rate": 0.0,
        "success_rate": 1.0,
        "health_ok": False,
    })

    assert health_failure["risk_level"] == "HIGH"
    assert health_failure["risk_percent"] >= 65
    assert healthy["risk_level"] == "LOW"
    assert healthy["risk_percent"] < 35
    assert unhealthy["risk_level"] == "HIGH"
    assert unhealthy["risk_percent"] >= 65


def test_high_resource_risk_stops_canary(monkeypatch):
    class FakeResponse:
        status_code = 200
        elapsed = type("Elapsed", (), {"total_seconds": lambda self: 0.001})()

        @staticmethod
        def json():
            return {"version": "v1"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url):
            return FakeResponse()

    async def healthy_check(base_url, path, expected_version=None):
        return True, "health passed"

    monkeypatch.setattr(
        controller.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(controller, "check_endpoint", healthy_check)
    monkeypatch.setattr(
        controller,
        "get_v2_resource_metrics",
        lambda _container_name=None: {"cpu_percent": 95, "memory_percent": 95},
    )

    success, message = asyncio.run(controller.monitor_canary(10, 0.001))

    assert not success
    assert "CPU usage" in message
    assert "90.0% limit" in message
