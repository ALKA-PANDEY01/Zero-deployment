import os
import threading
import time

import httpx
import pytest


CONTROLLER_URL = os.getenv("E2E_CONTROLLER_URL")
GATEWAY_URL = os.getenv("E2E_GATEWAY_URL")


@pytest.mark.skipif(
    not CONTROLLER_URL or not GATEWAY_URL,
    reason="Set E2E_CONTROLLER_URL and E2E_GATEWAY_URL to run Compose integration tests",
)
def test_proxy_serves_traffic_through_canary_deployment():
    controller_url = CONTROLLER_URL.rstrip("/")
    gateway_url = GATEWAY_URL.rstrip("/")
    timeout = httpx.Timeout(3)

    status = httpx.get(f"{controller_url}/status", timeout=timeout).json()
    assert not status.get("active"), "A deployment is already running"
    assert status["status"] not in {"validating", "rolling_back"}
    previous_started_at = status.get("started_at")

    # Restore the stable route before checking the gateway: an earlier
    # successful rollout may have left the public endpoint on V2.
    rollback = httpx.post(f"{controller_url}/rollback", timeout=timeout)
    assert rollback.status_code == 200

    ready_deadline = time.monotonic() + 30
    baseline = None
    while time.monotonic() < ready_deadline:
        try:
            candidate = httpx.get(f"{gateway_url}/health", timeout=timeout)
            if (
                candidate.status_code == 200
                and candidate.json().get("version") == "v1"
            ):
                baseline = candidate
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    assert baseline is not None, "V1 was not ready before starting the rollout"

    baseline = httpx.get(f"{gateway_url}/health", timeout=timeout)
    assert baseline.status_code == 200
    assert baseline.json()["version"] == "v1"

    rollout_active = threading.Event()
    stop_probes = threading.Event()
    probe_results = []

    def probe_proxy():
        with httpx.Client(timeout=1) as client:
            while not stop_probes.is_set():
                try:
                    response = client.get(
                        f"{gateway_url}/",
                        headers={"Connection": "close"},
                    )
                    result = (
                        response.status_code,
                        response.json().get("version"),
                    )
                except Exception as error:  # Capture outages for the assertion below.
                    result = (None, str(error))

                if rollout_active.is_set():
                    probe_results.append(result)
                time.sleep(0.05)

    probe_thread = threading.Thread(target=probe_proxy, daemon=True)
    probe_thread.start()

    try:
        start = httpx.post(
            f"{controller_url}/deploy",
            json={"observation_seconds": 1},
            timeout=timeout,
        )
        assert start.status_code == 200, start.text

        deadline = time.monotonic() + 75
        final_status = None
        while time.monotonic() < deadline:
            current = httpx.get(
                f"{controller_url}/status",
                timeout=timeout,
            ).json()
            deployment_is_current = (
                current.get("started_at") != previous_started_at
            )
            if deployment_is_current and current.get("active"):
                rollout_active.set()

            if deployment_is_current and current.get("finished_at"):
                final_status = current
                break
            time.sleep(0.1)

        assert final_status is not None, "Deployment did not finish in time"
        assert final_status["status"] == "succeeded"
        phase = httpx.get(
            f"{controller_url}/api/v1/deployment/state",
            timeout=timeout,
        ).json()
        assert phase["phase"] == "SUCCESS"
        assert probe_results, "No proxy samples were captured during deployment"
        assert all(result[0] == 200 for result in probe_results), probe_results

        messages = [step["message"] for step in final_status["steps"]]
        assert "V2 test passed: /health" in messages
        assert "Canary stage started: V2=10%" in messages
        assert "Canary stage started: V2=25%" in messages
        assert "Canary stage started: V2=50%" in messages
        assert "Canary stage started: V2=100%" in messages
    finally:
        stop_probes.set()
        probe_thread.join(timeout=5)
        httpx.post(f"{controller_url}/rollback", timeout=timeout)
        removed = httpx.delete(
            f"{controller_url}/api/v1/versions/v2",
            timeout=timeout,
        )
        assert removed.status_code in {200, 404}, removed.text


@pytest.mark.skipif(
    not CONTROLLER_URL or not GATEWAY_URL,
    reason="Set E2E_CONTROLLER_URL and E2E_GATEWAY_URL to run Compose integration tests",
)
def test_demo_health_failure_automatically_rolls_back_and_preserves_logs():
    controller_url = CONTROLLER_URL.rstrip("/")
    gateway_url = GATEWAY_URL.rstrip("/")
    timeout = httpx.Timeout(10)
    previous_started_at = httpx.get(
        f"{controller_url}/status",
        timeout=timeout,
    ).json()["started_at"]

    start = httpx.post(
        f"{controller_url}/demo/failure",
        json={"observation_seconds": 1},
        timeout=timeout,
    )
    assert start.status_code == 200, start.text

    deadline = time.monotonic() + 120
    final_status = None
    while time.monotonic() < deadline:
        current = httpx.get(
            f"{controller_url}/deployment/status",
            timeout=timeout,
        ).json()
        if (
            current["started_at"] != previous_started_at
            and current["finished_at"] is not None
        ):
            final_status = current
            break
        time.sleep(0.25)

    assert final_status is not None, "Failure demo did not finish in time"
    assert final_status["status"] == "ROLLBACK"
    assert final_status["traffic"] == 0
    assert final_status["health"] == "unhealthy"
    assert final_status["rollback"]["status"] == "ROLLBACK"
    assert final_status["rollback"]["reason"] == "health_check_failed"
    assert final_status["rollback"]["detail"].startswith(
        "/health returned HTTP 500"
    )
    assert final_status["rollback"]["failed_version"] == "v2"
    assert final_status["rollback"]["traffic_at_failure"] == 25
    assert final_status["rollback"]["stable_version"] == "v1"
    assert final_status["metrics"]["health_ok"] is False
    assert "GET /health HTTP/1.1\" 500" in final_status["candidate_logs"]
    messages = [
        step["message"]
        for step in httpx.get(
            f"{controller_url}/status",
            timeout=timeout,
        ).json()["steps"]
    ]
    assert "Canary stage started: V2=10%" in messages
    assert "Canary stage started: V2=25%" in messages
    assert not any("Canary stage started: V2=50%" in message for message in messages)

    gateway = httpx.get(f"{gateway_url}/health", timeout=timeout)
    assert gateway.status_code == 200
    assert gateway.json()["version"] == "v1"
    removed = httpx.get(
        f"{controller_url}/api/v1/versions/v2",
        timeout=timeout,
    )
    assert removed.status_code == 404


@pytest.mark.skipif(
    not CONTROLLER_URL or not GATEWAY_URL,
    reason="Set E2E_CONTROLLER_URL and E2E_GATEWAY_URL to run Compose integration tests",
)
def test_docker_version_api_runs_v1_and_v2_independently():
    controller_url = CONTROLLER_URL.rstrip("/")
    timeout = httpx.Timeout(120)
    built_versions = []
    started_versions = []

    try:
        for version in ("v1", "v2"):
            built = httpx.post(
                f"{controller_url}/api/v1/versions/{version}/build",
                timeout=timeout,
            )
            assert built.status_code == 200, built.text
            built_versions.append(version)

        for version in ("v1", "v2"):
            started = httpx.post(
                f"{controller_url}/api/v1/versions/{version}/start",
                timeout=timeout,
            )
            assert started.status_code == 200, started.text
            started_versions.append(version)
            assert started.json()["status"] == "running"

        for version in ("v1", "v2"):
            validation = httpx.post(
                f"{controller_url}/api/v1/versions/{version}/validate",
                timeout=timeout,
            )
            assert validation.status_code == 200, validation.text
            assert validation.json()["status"] == "HEALTHY"
            assert validation.json()["healthy"] is True

            inspected = httpx.get(
                f"{controller_url}/api/v1/versions/{version}",
                timeout=timeout,
            )
            assert inspected.status_code == 200, inspected.text
            assert inspected.json()["status"] == "running"
            health_deadline = time.monotonic() + 35
            while (
                inspected.json()["health"] == "starting"
                and time.monotonic() < health_deadline
            ):
                time.sleep(0.5)
                inspected = httpx.get(
                    f"{controller_url}/api/v1/versions/{version}",
                    timeout=timeout,
                )
                assert inspected.status_code == 200, inspected.text
            assert inspected.json()["health"] == "healthy"

            application = httpx.get(
                f"{inspected.json()['url']}/version",
                timeout=httpx.Timeout(5),
            )
            assert application.status_code == 200
            assert application.json() == {"version": version}

            logs = httpx.get(
                f"{controller_url}/api/v1/versions/{version}/logs",
                timeout=timeout,
            )
            assert logs.status_code == 200, logs.text
            assert "Uvicorn running" in logs.json()["logs"]

        stopped_v1 = httpx.post(
            f"{controller_url}/api/v1/versions/v1/stop",
            timeout=timeout,
        )
        assert stopped_v1.status_code == 200, stopped_v1.text
        still_running_v2 = httpx.get(
            f"{controller_url}/api/v1/versions/v2",
            timeout=timeout,
        )
        assert still_running_v2.status_code == 200
        assert still_running_v2.json()["status"] == "running"
    finally:
        for version in reversed(built_versions):
            httpx.delete(
                f"{controller_url}/api/v1/versions/{version}",
                timeout=timeout,
            )


@pytest.mark.skipif(
    not CONTROLLER_URL or not GATEWAY_URL,
    reason="Set E2E_CONTROLLER_URL and E2E_GATEWAY_URL to run Compose integration tests",
)
def test_public_gateway_distributes_traffic_at_configured_percentages():
    controller_url = CONTROLLER_URL.rstrip("/")
    gateway_url = GATEWAY_URL.rstrip("/")
    timeout = httpx.Timeout(5)

    with httpx.Client(timeout=timeout) as client:
        try:
            for v2_percent in (0, 10, 25, 50, 100):
                update = client.put(
                    f"{controller_url}/deployment/traffic",
                    json={"v2_percent": v2_percent},
                )
                assert update.status_code == 200, update.text
                assert update.json() == {
                    "v1_percent": 100 - v2_percent,
                    "v2_percent": v2_percent,
                }
                time.sleep(1)

                distribution = {"v1": 0, "v2": 0}
                sample_count = 100
                for _ in range(sample_count):
                    response = client.get(
                        f"{gateway_url}/",
                        headers={"Connection": "close"},
                    )
                    assert response.status_code == 200
                    version = response.json()["version"]
                    distribution[version] += 1

                print(f"Configured V2={v2_percent}%: observed {distribution}")
                if v2_percent == 0:
                    assert distribution == {"v1": sample_count, "v2": 0}
                elif v2_percent == 100:
                    assert distribution == {"v1": 0, "v2": sample_count}
                elif v2_percent == 10:
                    assert 5 <= distribution["v2"] <= 15, distribution
                elif v2_percent == 25:
                    assert 20 <= distribution["v2"] <= 30, distribution
                else:
                    assert 40 <= distribution["v2"] <= 60, distribution

                current = client.get(
                    f"{controller_url}/deployment/traffic",
                )
                assert current.json() == update.json()
        finally:
            reset = client.put(
                f"{controller_url}/deployment/traffic",
                json={"v2_percent": 0},
            )
            assert reset.status_code == 200, reset.text
