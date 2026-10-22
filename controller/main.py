import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

if __package__:
    from .zerodown.api import router as architecture_api
    from .zerodown.config import load_config
    from .zerodown.state import DeploymentPhase, DeploymentStateStore
else:
    from zerodown.api import router as architecture_api
    from zerodown.config import load_config
    from zerodown.state import DeploymentPhase, DeploymentStateStore

try:
    from .risk_model import predict_deployment_risk
except ImportError:
    from risk_model import predict_deployment_risk


DASHBOARD_HTML = Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8')


app = FastAPI(
    title="Zero-Downtime Deployment Controller",
    version="1.0.0",
)
app.state.zerodown_config = load_config()
app.state.deployment_state_store = DeploymentStateStore()
app.include_router(architecture_api)


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

CONFIG = os.getenv(
    "CONFIG",
    os.getenv("PROXY_CONFIG", "/shared/nginx.conf"),
)

PROXY_CONTAINER = os.getenv(
    "PROXY_CONTAINER",
    "zerodown-proxy"
)

V1_URL = os.getenv(
    "V1_URL",
    "http://app_v1:8000"
)

V2_URL = os.getenv(
    "V2_URL",
    "http://app_v2:8000"
)

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://proxy"
)

V2_CONTAINER = os.getenv("V2_CONTAINER", "zerodown-app-v2")
RISK_TELEMETRY_PATH = os.getenv("RISK_TELEMETRY_PATH")


# --------------------------------------------------
# DEPLOYMENT STATE
# --------------------------------------------------

deployment_state = {
    "status": "idle",
    "message": "Ready",
    "steps": [],
    "started_at": None,
    "finished_at": None,
}


deployment_lock = asyncio.Lock()


class DeployRequest(BaseModel):
    observation_seconds: int | None = None


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

def record(
    message: str,
    status: str | None = None,
    phase: DeploymentPhase | None = None,
):

    timestamp = datetime.now(timezone.utc).isoformat()
    deployment_state["steps"].append(
        {
            "time": timestamp,
            "message": message,
        }
    )

    print(f"[{timestamp}] {message}", flush=True)

    if status:
        deployment_state["status"] = status
    if phase is not None:
        app.state.deployment_state_store.set_phase(phase, message)


# --------------------------------------------------
# CHANGE NGINX TRAFFIC
# --------------------------------------------------

def set_traffic(v1: int, v2: int):

    if v1 + v2 != 100:
        raise ValueError(
            "Traffic percentages must total 100"
        )

    upstream_servers = []
    if v1:
        upstream_servers.append(
            f"        server app_v1:8000 weight={v1} "
            "max_fails=2 fail_timeout=5s;"
        )
    if v2:
        upstream_servers.append(
            f"        server app_v2:8000 weight={v2} "
            "max_fails=2 fail_timeout=5s;"
        )
    upstream_config = "\n".join(upstream_servers)

    nginx_config = f"""
worker_processes auto;

events {{
    worker_connections 1024;
}}

http {{

    access_log /dev/stdout;
    error_log /dev/stderr warn;

    upstream app_pool {{

{upstream_config}
    }}

    server {{

        listen 80;

        location / {{

            proxy_pass http://app_pool;

            proxy_set_header Host $host;

            proxy_set_header X-Real-IP $remote_addr;

            proxy_connect_timeout 2s;

            proxy_read_timeout 5s;
        }}
    }}
}}
"""

    with open(CONFIG, "w") as file:
        file.write(nginx_config)

    docker_client = docker.from_env(timeout=3)

    proxy = docker_client.containers.get(
        PROXY_CONTAINER
    )

    # Test configuration first
    result = proxy.exec_run(
        "nginx -t"
    )

    if result.exit_code != 0:

        output = result.output.decode(
            errors="replace"
        )

        raise RuntimeError(
            f"Nginx configuration failed: {output}"
        )

    # Gracefully reload Nginx
    result = proxy.exec_run(
        "nginx -s reload"
    )

    if result.exit_code != 0:

        output = result.output.decode(
            errors="replace"
        )

        raise RuntimeError(
            f"Nginx reload failed: {output}"
        )

    record(
        f"Traffic changed: V1={v1}% | V2={v2}%"
    )


# --------------------------------------------------
# HTTP TEST
# --------------------------------------------------

async def check_endpoint(
    base_url: str,
    path: str,
    expected_version: str | None = None,
):

    try:

        async with httpx.AsyncClient(
            timeout=3
        ) as client:

            response = await client.get(
                f"{base_url}{path}"
            )

        if response.status_code != 200:

            return (
                False,
                f"{path} returned HTTP "
                f"{response.status_code}",
            )

        data = response.json()

        if expected_version:

            if data.get("version") != expected_version:

                return (
                    False,
                    f"{path}: expected "
                    f"{expected_version}, got "
                    f"{data.get('version')}",
                )

        return (
            True,
            f"{path} passed",
        )

    except Exception as error:

        return (
            False,
            f"{path} failed: {error}",
        )


# --------------------------------------------------
# V2 VALIDATION
# --------------------------------------------------

async def validate_v2():

    config = app.state.zerodown_config
    tests = [config.health.endpoint, *config.health.smoke_endpoints]

    for path in tests:

        success, message = await check_endpoint(
            V2_URL,
            path,
            "v2",
        )

        if not success:

            return False, message

        record(
            f"V2 test passed: {path}"
        )

    return (
        True,
        "All V2 validation tests passed",
    )


async def set_v2_demo_failure(enabled: bool):
    """Toggle V2's local demo-only health failure switch."""
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.post(
            f"{V2_URL}/demo/failure/{str(enabled).lower()}"
        )
    response.raise_for_status()


def get_v2_resource_metrics():
    """Read current V2 CPU and memory usage from Docker stats."""
    try:
        container = docker.from_env(timeout=3).containers.get(V2_CONTAINER)
        stats = container.stats(stream=False)
        cpu_stats = stats.get("cpu_stats", {})
        previous_cpu = stats.get("precpu_stats", {})
        cpu_usage = cpu_stats.get("cpu_usage", {})
        previous_usage = previous_cpu.get("cpu_usage", {})
        cpu_delta = cpu_usage.get("total_usage", 0) - previous_usage.get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - previous_cpu.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(cpu_usage.get("percpu_usage", [])) or 1
        cpu_percent = (cpu_delta / system_delta * online_cpus * 100) if system_delta > 0 else None

        memory_stats = stats.get("memory_stats", {})
        memory_usage = memory_stats.get("usage")
        memory_limit = memory_stats.get("limit")
        memory_percent = (memory_usage / memory_limit * 100) if memory_usage is not None and memory_limit else None
        return {
            "cpu_percent": max(0.0, min(100.0, cpu_percent)) if cpu_percent is not None else None,
            "memory_percent": max(0.0, min(100.0, memory_percent)) if memory_percent is not None else None,
        }
    except Exception as error:
        return {"cpu_percent": None, "memory_percent": None, "resource_error": str(error)}


def percentile_95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def persist_risk_observation(metrics, prediction):
    """Append a real canary observation for later model calibration."""
    if not RISK_TELEMETRY_PATH:
        return
    observation = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "deployment_started_at": deployment_state.get("started_at"),
        "metrics": metrics,
        "prediction": prediction,
    }
    telemetry_path = Path(RISK_TELEMETRY_PATH)
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    with telemetry_path.open("a", encoding="utf-8") as telemetry_file:
        telemetry_file.write(json.dumps(observation, sort_keys=True) + "\n")

async def score_canary_metrics(metrics):
    resource_metrics = await asyncio.to_thread(get_v2_resource_metrics)
    metrics.update(resource_metrics)
    prediction = predict_deployment_risk(metrics)
    if RISK_TELEMETRY_PATH:
        try:
            await asyncio.to_thread(persist_risk_observation, metrics, prediction)
        except OSError as error:
            record(f"Risk telemetry could not be saved: {error}")
    record(
        f"Deployment risk: {prediction['risk_percent']}% - "
        f"{prediction['risk_level']}"
    )
    record(
        "ML metrics: "
        f"CPU={metrics.get('cpu_percent')}, "
        f"memory={metrics.get('memory_percent')}, "
        f"p95_latency_ms={metrics.get('p95_latency_ms', 0):.1f}, "
        f"error_rate={metrics.get('error_rate', 0):.1%}, "
        f"http_5xx_rate={metrics.get('http_5xx_rate', 0):.1%}, "
        f"success_rate={metrics.get('success_rate', 0):.1%}, "
        f"health={'pass' if metrics.get('health_ok') else 'fail'}"
    )
    return prediction


def safety_limit_failure(metrics, prediction):
    limits = app.state.zerodown_config.limits
    error_rate_percent = metrics.get("error_rate", 1.0) * 100
    if error_rate_percent > limits.max_error_rate:
        return (
            f"Error rate {error_rate_percent:.1f}% exceeded "
            f"the {limits.max_error_rate:.1f}% limit"
        )

    latency_ms = metrics.get("p95_latency_ms")
    if latency_ms is not None and latency_ms > limits.max_latency_ms:
        return (
            f"p95 latency {latency_ms:.1f}ms exceeded "
            f"the {limits.max_latency_ms:.1f}ms limit"
        )

    cpu_percent = metrics.get("cpu_percent")
    if cpu_percent is not None and cpu_percent > limits.max_cpu_percent:
        return (
            f"CPU usage {cpu_percent:.1f}% exceeded "
            f"the {limits.max_cpu_percent:.1f}% limit"
        )

    memory_percent = metrics.get("memory_percent")
    if memory_percent is not None and memory_percent > limits.max_memory_percent:
        return (
            f"Memory usage {memory_percent:.1f}% exceeded "
            f"the {limits.max_memory_percent:.1f}% limit"
        )

    risk_percent = prediction["probability"] * 100
    if risk_percent >= limits.max_risk_percent:
        return (
            f"ML risk {prediction['risk_percent']}% "
            f"{prediction['risk_level']} exceeded the "
            f"{limits.max_risk_percent:.1f}% limit"
        )
    return None


async def monitor_canary(
    v2_percent: int,
    observation_seconds: float,
    simulate_failure: bool = False,
):
    """Sample release signals and stop the canary when predicted risk is high."""
    requests = 0
    errors = 0
    server_errors = 0
    successes = 0
    v1_responses = 0
    v2_responses = 0
    health_checks = 0
    health_failures = 0
    latencies_ms = []
    failure_injected = False
    started = time.monotonic()
    deadline = started + observation_seconds

    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started
            if (
                simulate_failure
                and not failure_injected
                and elapsed >= min(2, observation_seconds / 2)
            ):
                await set_v2_demo_failure(True)
                failure_injected = True
                record("Demo injected V2 health failure")

            health_started = time.perf_counter()
            healthy, health_message = await check_endpoint(
                V2_URL,
                app.state.zerodown_config.health.endpoint,
                "v2",
            )
            latencies_ms.append((time.perf_counter() - health_started) * 1000)
            health_checks += 1
            if not healthy:
                health_failures += 1
                metrics = {
                    "requests": requests,
                    "error_rate": errors / requests if requests else 1.0,
                    "http_5xx_rate": server_errors / requests if requests else 1.0,
                    "success_rate": successes / requests if requests else 0.0,
                    "p95_latency_ms": percentile_95(latencies_ms),
                    "health_ok": False,
                }
                prediction = await score_canary_metrics(metrics)
                record(
                    f"Canary monitor failed at V2={v2_percent}%: "
                    f"{health_message}"
                )
                return False, (
                    f"{health_message}; ML risk "
                    f"{prediction['risk_percent']}% {prediction['risk_level']}"
                )

            requests += 1
            try:
                response = await client.get(f"{GATEWAY_URL}/")
                latencies_ms.append(response.elapsed.total_seconds() * 1000)
                if response.status_code != 200:
                    errors += 1
                    if response.status_code >= 500:
                        server_errors += 1
                else:
                    successes += 1
                    version = response.json().get("version")
                    if version == "v1":
                        v1_responses += 1
                    elif version == "v2":
                        v2_responses += 1
                    else:
                        errors += 1
                        successes -= 1
            except Exception:
                errors += 1

            await asyncio.sleep(0.2)

    error_rate = errors / requests if requests else 1.0
    metrics = {
        "requests": requests,
        "error_rate": error_rate,
        "http_5xx_rate": server_errors / requests if requests else 1.0,
        "success_rate": successes / requests if requests else 0.0,
        "p95_latency_ms": percentile_95(latencies_ms),
        "health_ok": health_failures == 0 and health_checks > 0,
    }
    prediction = await score_canary_metrics(metrics)
    message = (
        f"V2={v2_percent}%: {requests} gateway requests, "
        f"V1 responses={v1_responses}, V2 responses={v2_responses}, "
        f"errors={errors} ({error_rate:.1%}), "
        f"risk={prediction['risk_percent']}% {prediction['risk_level']}"
    )
    record(f"Canary metrics: {message}")

    if not requests:
        return False, "Canary monitor received no gateway responses"
    limit_failure = safety_limit_failure(metrics, prediction)
    if limit_failure:
        return False, limit_failure
    if simulate_failure and not failure_injected:
        return False, "Demo failed to inject the V2 health failure"

    return True, message


# --------------------------------------------------
# ROLLBACK
# --------------------------------------------------

def rollback():

    record(
        "Starting automatic rollback",
        "rolling_back",
        DeploymentPhase.ROLLBACK,
    )

    set_traffic(
        100,
        0,
    )

    record(
        "Rollback complete: V1 receives 100% traffic",
        "rolled_back",
    )


# --------------------------------------------------
# DEPLOYMENT PROCESS
# --------------------------------------------------

async def deployment_job(
    observation_seconds: int,
    simulate_failure: bool = False,
):

    async with deployment_lock:

        deployment_state["steps"] = []

        deployment_state["started_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        deployment_state["finished_at"] = None

        try:

            if simulate_failure:
                await set_v2_demo_failure(False)
                record("Failure demo armed: V2 starts healthy")

            record(
                "Deployment started",
                "validating",
                DeploymentPhase.HEALTH_CHECK,
            )

            # --------------------------------------
            # STEP 1
            # Validate V2
            # --------------------------------------

            success, message = (
                await validate_v2()
            )

            if not success:

                record(
                    f"V2 validation failed: {message}"
                )

                rollback()

                return

            record(
                "V2 passed health, readiness and smoke tests"
            )

            # --------------------------------------
            # GRADUAL CANARY ROLLOUT
            # --------------------------------------

            for v2_percent in app.state.zerodown_config.rollout.stages:
                phase = {
                    10: DeploymentPhase.CANARY_10,
                    25: DeploymentPhase.CANARY_25,
                    50: DeploymentPhase.CANARY_50,
                    100: DeploymentPhase.FULL_ROLLOUT,
                }[v2_percent]
                set_traffic(
                    100 - v2_percent,
                    v2_percent,
                )
                record(
                    f"Canary stage started: V2={v2_percent}%",
                    phase=phase,
                )

                success, message = await monitor_canary(
                    v2_percent,
                    app.state.zerodown_config.rollout.stage_seconds,
                )
                if not success:
                    record(f"Canary stage failed: {message}")
                    rollback()
                    return

                record(
                    f"Canary stage passed: V2={v2_percent}%"
                )

            # --------------------------------------
            # OBSERVATION
            # --------------------------------------

            record(
                f"Observing V2 for "
                f"{observation_seconds} seconds"
            )

            success, message = await monitor_canary(
                100,
                observation_seconds,
                simulate_failure=simulate_failure,
            )
            if not success:
                record(f"Observation failed: {message}")
                rollback()
                if simulate_failure:
                    await set_v2_demo_failure(False)
                    record("V2 demo health restored after rollback")
                return

            record(
                "V2 promoted to 100% traffic",
                "succeeded",
                DeploymentPhase.SUCCESS,
            )

        except Exception as error:

            record(
                f"Deployment error: {error}"
            )

            try:

                rollback()

            except Exception as rollback_error:

                record(
                    "CRITICAL: rollback failed: "
                    f"{rollback_error}",
                    "failed",
                    DeploymentPhase.FAILED,
                )

        finally:

            deployment_state["finished_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )


# --------------------------------------------------
# DASHBOARD
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def deployment_dashboard():
    return DASHBOARD_HTML


@app.get("/dashboard/proxy-health")
async def dashboard_proxy_health():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{GATEWAY_URL}/health")
        payload = response.json()
        return {
            "healthy": response.status_code == 200,
            "status_code": response.status_code,
            "version": payload.get("version"),
        }
    except Exception as error:
        return {
            "healthy": False,
            "error": str(error),
        }


# --------------------------------------------------
# CONTROLLER HEALTH
# --------------------------------------------------
@app.get("/health")
def controller_health():

    return {
        "status": "ok",
        "service": "deployment-controller",
    }


# --------------------------------------------------
# DEPLOYMENT STATUS
# --------------------------------------------------

@app.get("/status")
def deployment_status():

    return deployment_state


# --------------------------------------------------
# START DEPLOYMENT
# --------------------------------------------------

@app.post("/deploy")
async def start_deployment(
    request: DeployRequest,
    background_tasks: BackgroundTasks,
):

    if deployment_state["status"] in [
        "validating",
        "rolling_back",
    ]:

        raise HTTPException(
            status_code=409,
            detail="Deployment already running",
        )

    observation_seconds = (
        request.observation_seconds
        if request.observation_seconds is not None
        else app.state.zerodown_config.rollout.observation_seconds
    )

    if not (
        1 <= observation_seconds <= 300
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "observation_seconds must "
                "be between 1 and 300"
            ),
        )

    background_tasks.add_task(
        deployment_job,
        observation_seconds,
    )

    return {
        "accepted": True,
        "message": (
            "Deployment started. "
            "Check /status."
        ),
    }


@app.post("/demo/failure")
async def start_failure_demo(
    background_tasks: BackgroundTasks,
    request: DeployRequest | None = None,
):
    """Run a canary, fail V2 during observation, and demonstrate rollback."""
    if deployment_state["status"] in [
        "validating",
        "rolling_back",
    ]:
        raise HTTPException(
            status_code=409,
            detail="Deployment already running",
        )

    observation_seconds = (
        request.observation_seconds
        if request is not None
        and request.observation_seconds is not None
        else app.state.zerodown_config.rollout.observation_seconds
    )
    if not 1 <= observation_seconds <= 300:
        raise HTTPException(
            status_code=400,
            detail="observation_seconds must be between 1 and 300",
        )

    background_tasks.add_task(
        deployment_job,
        observation_seconds,
        True,
    )
    return {
        "accepted": True,
        "message": "Failure demo started. V2 will fail during observation; check /status.",
    }


# --------------------------------------------------
# MANUAL ROLLBACK
# --------------------------------------------------

@app.post("/rollback")
def manual_rollback():

    try:

        rollback()

        return {
            "status": "rolled_back",
            "v1": 100,
            "v2": 0,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )
