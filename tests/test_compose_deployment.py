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
    assert status["status"] not in {"validating", "rolling_back"}
    previous_started_at = status.get("started_at")

    # Establish a deterministic V1 baseline before starting the rollout.
    rollback = httpx.post(f"{controller_url}/rollback", timeout=timeout)
    assert rollback.status_code == 200
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
                    response = client.get(f"{gateway_url}/")
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
            if deployment_is_current and current["status"] in {
                "validating",
                "rolling_back",
            }:
                rollout_active.set()

            if deployment_is_current and current.get("finished_at"):
                final_status = current
                break
            time.sleep(0.1)

        assert final_status is not None, "Deployment did not finish in time"
        assert final_status["status"] == "succeeded"
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
