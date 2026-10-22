import asyncio
import hmac
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import docker
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

if __package__:
    from .zerodown.api import router as architecture_api
    from .zerodown.config import load_config, validate_rollout_stages
    from .zerodown.docker_manager import DockerManager
    from .zerodown.health_checks import ValidationResult, VersionHealthChecker
    from .zerodown.router import NginxTrafficRouter, traffic_api
    from .zerodown.state import DeploymentPhase, DeploymentStateStore
else:
    from zerodown.api import router as architecture_api
    from zerodown.config import load_config, validate_rollout_stages
    from zerodown.docker_manager import DockerManager
    from zerodown.health_checks import ValidationResult, VersionHealthChecker
    from zerodown.router import NginxTrafficRouter, traffic_api
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
app.state.docker_manager = DockerManager(
    project_root=Path(
        os.getenv(
            "ZERODOWN_PROJECT_ROOT",
            str(Path(__file__).resolve().parents[1]),
        )
    ),
    network=os.getenv("ZERODOWN_DOCKER_NETWORK", "zerodown"),
)
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

V1_URL = os.getenv("V1_URL")
V2_URL = os.getenv("V2_URL")

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://proxy"
)

V2_CONTAINER = os.getenv("V2_CONTAINER", "zerodown-app-v2")
RISK_TELEMETRY_PATH = os.getenv("RISK_TELEMETRY_PATH")

app.state.traffic_router = NginxTrafficRouter(
    config_path=Path(CONFIG),
    proxy_container=PROXY_CONTAINER,
    v1_target=os.getenv("ZERODOWN_V1_TARGET", "app_v1"),
    v2_target=os.getenv("ZERODOWN_V2_TARGET", "app_v2"),
    v1_container=os.getenv("ZERODOWN_V1_CONTAINER", "zerodown-app-v1"),
    v2_container=os.getenv("ZERODOWN_V2_CONTAINER", "zerodown-app-v2"),
    app_port=app.state.zerodown_config.app.port,
)
app.state.version_health_checker = VersionHealthChecker(
    docker_manager=app.state.docker_manager,
    config=app.state.zerodown_config,
)
app.include_router(traffic_api)


# --------------------------------------------------
# DEPLOYMENT STATE
# --------------------------------------------------

deployment_state = {
    "status": "idle",
    "phase": DeploymentPhase.IDLE.value,
    "message": "Ready",
    "steps": [],
    "started_at": None,
    "finished_at": None,
    "deployment_id": None,
    "version": "v2",
    "traffic": 0,
    "health": "unknown",
    "progress": 0,
    "metrics": {},
    "rollback": None,
    "candidate_logs": None,
    "active": False,
    "container_name": None,
}


deployment_lock = asyncio.Lock()
app.state.deployment_task = None


class DeployRequest(BaseModel):
    observation_seconds: int | None = None
    image: str | None = None
    registry_username: str | None = None
    registry_token: str | None = None


def app_url(version: str) -> str:
    configured_url = {"v1": V1_URL, "v2": V2_URL}[version]
    if configured_url:
        return configured_url
    port = app.state.zerodown_config.app.port
    return f"http://app_{version}:{port}"


async def validate_version(
    version: str,
    container_name: str | None = None,
    base_url: str | None = None,
) -> ValidationResult:
    return await app.state.version_health_checker.validate_version(
        version,
        container_name=container_name,
        base_url=base_url,
    )


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
    deployment_state["message"] = message

    print(f"[{timestamp}] {message}", flush=True)

    if status:
        deployment_state["status"] = status
    if phase is not None:
        deployment_state["phase"] = phase.value
        app.state.deployment_state_store.set_phase(phase, message)


# --------------------------------------------------
# CHANGE NGINX TRAFFIC
# --------------------------------------------------

def set_traffic(v1: int, v2: int):
    app.state.traffic_router.set_percentages(v1, v2)
    deployment_state["traffic"] = v2

    record(
        f"Traffic changed: V1={v1}% | V2={v2}%"
    )


def stage_phase(is_final_stage: bool) -> DeploymentPhase:
    return (
        DeploymentPhase.FULL_ROLLOUT
        if is_final_stage
        else DeploymentPhase.CANARY
    )


def candidate_container_name() -> str:
    config = app.state.zerodown_config
    return app.state.docker_manager.container_name(config.app.name, "v2")


def candidate_app_url() -> str:
    config = app.state.zerodown_config
    return app.state.docker_manager.app_url(
        config.app.name,
        "v2",
        config.app.port,
    )


async def build_and_start_v2(
    image_ref: str | None = None,
    registry_auth: dict[str, str] | None = None,
) -> None:
    config = app.state.zerodown_config
    manager = app.state.docker_manager
    container_name = candidate_container_name()
    image_tag = image_ref or manager.image_tag(config.app.name, "v2")

    if await asyncio.to_thread(manager.container_exists, container_name):
        await asyncio.to_thread(manager.remove_container, container_name)

    if image_ref:
        image_id = await asyncio.to_thread(
            manager.pull_image,
            image_ref,
            registry_auth,
        )
        record(
            f"Pulled V2 image {image_tag} ({image_id})",
            "starting",
            DeploymentPhase.STARTING,
        )
    else:
        image_id = await asyncio.to_thread(
            manager.build_image,
            config.app.build_context,
            config.app.dockerfile,
            image_tag,
        )
        record(
            f"Built V2 image {image_tag} ({image_id})",
            "starting",
            DeploymentPhase.STARTING,
        )
    details = await asyncio.to_thread(
        manager.start_container,
        image_tag,
        container_name,
        config.app.port,
        "v2",
        config.app.name,
        manager.host_port("v2"),
    )
    deployment_state["container_name"] = container_name
    app.state.traffic_router.set_version_target(
        "v2",
        container_name,
        container_name,
    )
    record(f"Started V2 container {details['name']}")


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
    container_name = candidate_container_name()
    result = await validate_version(
        "v2",
        container_name=container_name,
        base_url=candidate_app_url(),
    )
    deployment_state["health"] = "healthy" if result.healthy else "unhealthy"
    for check in result.checks:
        if check.get("endpoint") and check["status"] == "HEALTHY":
            record(f"V2 test passed: {check['endpoint']}")
        else:
            record(
                f"V2 {check['check']} validation: {check['status']} - "
                f"{check['message']}"
            )

    return result.healthy, result.message


async def set_v2_demo_failure(enabled: bool):
    """Toggle V2's local demo-only health failure switch."""
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.post(
            f"{candidate_app_url()}/demo/failure/{str(enabled).lower()}"
        )
    response.raise_for_status()


def get_v2_resource_metrics(container_name: str | None = None):
    """Read current V2 CPU and memory usage from Docker stats."""
    try:
        container = docker.from_env(timeout=3).containers.get(
            container_name or deployment_state.get("container_name") or V2_CONTAINER
        )
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
    resource_metrics = await asyncio.to_thread(
        get_v2_resource_metrics,
        deployment_state.get("container_name"),
    )
    metrics.update(resource_metrics)
    deployment_state["metrics"] = dict(metrics)
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
                candidate_app_url(),
                app.state.zerodown_config.health.endpoint,
                "v2",
            )
            latencies_ms.append((time.perf_counter() - health_started) * 1000)
            health_checks += 1
            if not healthy:
                health_failures += 1
                deployment_state["health"] = "unhealthy"
                metrics = {
                    "requests": requests,
                    "error_rate": errors / requests if requests else 1.0,
                    "http_5xx_rate": server_errors / requests if requests else 1.0,
                    "success_rate": successes / requests if requests else 0.0,
                    "p95_latency_ms": percentile_95(latencies_ms),
                    "health_ok": False,
                    "health_status": "unhealthy",
                    "health_error": health_message,
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
        "health_status": "healthy" if health_failures == 0 else "unhealthy",
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

def rollback(
    reason: str = "manual_rollback",
    failed_version: str = "v2",
    traffic_at_failure: int | None = None,
    cleanup_candidate: bool = False,
):
    if traffic_at_failure is None:
        traffic_at_failure = app.state.traffic_router.get_traffic()["v2_percent"]
    reason_code = reason_code_for(reason)
    record(
        "Starting automatic rollback",
        "rolling_back",
        DeploymentPhase.ROLLBACK,
    )

    set_traffic(100, 0)
    deployment_state["rollback"] = {
        "status": DeploymentPhase.ROLLBACK.value,
        "reason": reason_code,
        "detail": reason,
        "failed_version": failed_version,
        "traffic_at_failure": traffic_at_failure,
        "stable_version": "v1",
    }

    record(
        "Rollback complete: V1 receives 100% traffic",
        "rolled_back",
    )
    if cleanup_candidate:
        preserve_and_remove_candidate()


def reason_code_for(reason: str) -> str:
    normalized = reason.lower()
    if "error rate" in normalized or "http 5xx" in normalized:
        return "error_rate_exceeded"
    if "latency" in normalized or "timed out" in normalized:
        return "latency_exceeded"
    if "cpu" in normalized:
        return "cpu_exceeded"
    if "memory" in normalized:
        return "memory_exceeded"
    if "health" in normalized or "unhealthy" in normalized:
        return "health_check_failed"
    if "risk" in normalized:
        return "risk_threshold_exceeded"
    return "deployment_failed"


def preserve_and_remove_candidate() -> None:
    container_name = deployment_state.get("container_name")
    if not container_name:
        return

    manager = app.state.docker_manager
    try:
        deployment_state["candidate_logs"] = manager.container_logs(
            container_name,
            tail=500,
        )
    except (docker.errors.DockerException, ValueError) as error:
        record(f"Could not preserve failed V2 logs: {error}")

    try:
        app.state.traffic_router.reset_version_target("v2")
        manager.remove_container(container_name)
        record(f"Removed failed V2 container {container_name}")
    except (docker.errors.DockerException, ValueError) as error:
        record(f"Could not remove failed V2 container: {error}")


# --------------------------------------------------
# DEPLOYMENT PROCESS
# --------------------------------------------------

async def deployment_job(
    observation_seconds: int,
    simulate_failure: bool = False,
    image_ref: str | None = None,
    registry_auth: dict[str, str] | None = None,
):

    async with deployment_lock:
        deployment_state["active"] = True
        deployment_state["deployment_id"] = (
            deployment_state.get("deployment_id") or str(uuid.uuid4())
        )
        deployment_state["steps"] = []
        deployment_state["progress"] = 0
        deployment_state["traffic"] = 0
        deployment_state["health"] = "unknown"
        deployment_state["metrics"] = {}
        deployment_state["rollback"] = None
        deployment_state["candidate_logs"] = None
        deployment_state["container_name"] = None

        deployment_state["started_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )
        deployment_state["finished_at"] = None

        try:
            stages = validate_rollout_stages(
                app.state.zerodown_config.rollout.stages
            )
            set_traffic(100, 0)

            if simulate_failure:
                record("Failure demo armed: V2 starts healthy")

            record(
                "Deployment started",
                "building",
                DeploymentPhase.BUILDING,
            )
            if image_ref is None:
                await build_and_start_v2()
            else:
                await build_and_start_v2(image_ref, registry_auth)

            record(
                "V2 container started; validating candidate",
                "validating",
                DeploymentPhase.HEALTH_CHECK,
            )

            success, message = await validate_v2()

            if not success:
                record(
                    f"V2 validation failed: {message}"
                )
                deployment_state["health"] = "unhealthy"
                rollback(
                    message,
                    traffic_at_failure=0,
                    cleanup_candidate=True,
                )
                return

            record(
                "V2 passed health, readiness and smoke tests"
            )

            for stage_index, v2_percent in enumerate(stages, start=1):
                is_final_stage = stage_index == len(stages)
                phase = stage_phase(is_final_stage)
                phase_name = (
                    DeploymentPhase.FULL_ROLLOUT.value
                    if is_final_stage
                    else f"CANARY_{v2_percent}"
                )
                set_traffic(
                    100 - v2_percent,
                    v2_percent,
                )
                record(
                    f"Canary stage started: V2={v2_percent}%",
                    phase=phase,
                )
                deployment_state["phase"] = phase_name
                deployment_state["status"] = phase_name.lower()
                deployment_state["progress"] = round(
                    stage_index / len(stages) * 100
                )

                failure_stage = min(2, len(stages))
                if simulate_failure and stage_index == failure_stage:
                    await set_v2_demo_failure(True)
                    deployment_state["health"] = "unhealthy"
                    record(
                        f"Demo injected V2 health failure at V2={v2_percent}%"
                    )

                success, message = await monitor_canary(
                    v2_percent,
                    app.state.zerodown_config.rollout.stage_seconds,
                )
                if not success:
                    deployment_state["health"] = "unhealthy"
                    record(f"Canary stage failed: {message}")
                    rollback(
                        message,
                        traffic_at_failure=v2_percent,
                        cleanup_candidate=True,
                    )
                    return

                record(
                    f"Canary stage passed: V2={v2_percent}%"
                )

            record(
                f"Observing V2 for "
                f"{observation_seconds} seconds"
            )

            success, message = await monitor_canary(
                stages[-1],
                observation_seconds,
                simulate_failure=simulate_failure,
            )
            if not success:
                deployment_state["health"] = "unhealthy"
                record(f"Observation failed: {message}")
                rollback(
                    message,
                    traffic_at_failure=stages[-1],
                    cleanup_candidate=True,
                )
                return

            record(
                f"V2 promoted to {stages[-1]}% traffic",
                "succeeded",
                DeploymentPhase.SUCCESS,
            )
            deployment_state["phase"] = DeploymentPhase.SUCCESS.value
            deployment_state["progress"] = 100

        except asyncio.CancelledError:
            record("Deployment interrupted; restoring stable V1 traffic")
            try:
                rollback(
                    "Deployment interrupted",
                    traffic_at_failure=deployment_state["traffic"],
                    cleanup_candidate=True,
                )
                deployment_state["status"] = "interrupted"
                deployment_state["phase"] = DeploymentPhase.ROLLBACK.value
                deployment_state["message"] = (
                    "Deployment interrupted; V1 restored"
                )
            except Exception as rollback_error:
                record(
                    f"CRITICAL: rollback after interruption failed: {rollback_error}",
                    "failed",
                    DeploymentPhase.FAILED,
                )
            raise

        except Exception as error:
            record(f"Deployment error: {error}")
            try:
                deployment_state["health"] = "unhealthy"
                rollback(
                    str(error),
                    traffic_at_failure=deployment_state["traffic"],
                    cleanup_candidate=True,
                )
            except Exception as rollback_error:
                record(
                    "CRITICAL: rollback failed: "
                    f"{rollback_error}",
                    "failed",
                    DeploymentPhase.FAILED,
                )
            else:
                record(
                    f"Deployment failed: {error}",
                    "failed",
                    DeploymentPhase.FAILED,
                )

        finally:
            deployment_state["finished_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )
            deployment_state["active"] = False
            if app.state.deployment_task is asyncio.current_task():
                app.state.deployment_task = None


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


@app.get("/dashboard/versions")
def dashboard_versions():
    manager = app.state.docker_manager
    traffic_router = app.state.traffic_router
    versions = {}
    for version in ("v1", "v2"):
        container_name = traffic_router.targets[version][1]
        try:
            details = manager.inspect_container(container_name)
        except ValueError:
            details = None
        except docker.errors.DockerException as error:
            details = {
                "name": container_name,
                "status": "error",
                "health": "unknown",
                "error": str(error),
            }
        versions[version] = details
    return versions


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


@app.get("/deployment/status")
def deployment_progress_status():
    traffic = app.state.traffic_router.get_traffic()
    state = deployment_state["status"]
    stable_version = (
        "v2"
        if state == "succeeded" and traffic["v2_percent"] == 100
        else "v1"
    )
    return {
        "status": deployment_state["phase"],
        "application": app.state.zerodown_config.app.name,
        "deployment_id": deployment_state["deployment_id"],
        "stable_version": stable_version,
        "candidate_version": deployment_state["version"],
        "stages": list(app.state.zerodown_config.rollout.stages),
        "events": deployment_state["steps"],
        "version": deployment_state["version"],
        "traffic": traffic["v2_percent"],
        "health": deployment_state["health"],
        "progress": deployment_state["progress"],
        "metrics": deployment_state["metrics"],
        "rollback": deployment_state["rollback"],
        "candidate_logs": deployment_state["candidate_logs"],
        "message": deployment_state["message"],
        "started_at": deployment_state["started_at"],
        "finished_at": deployment_state["finished_at"],
    }


# --------------------------------------------------
# START DEPLOYMENT
# --------------------------------------------------

def schedule_deployment(
    observation_seconds: int,
    simulate_failure: bool = False,
    image_ref: str | None = None,
    registry_auth: dict[str, str] | None = None,
):
    if deployment_state["active"]:
        raise HTTPException(
            status_code=409,
            detail="Deployment already running",
        )

    if not 1 <= observation_seconds <= 300:
        raise HTTPException(
            status_code=400,
            detail="observation_seconds must be between 1 and 300",
        )

    deployment_state["active"] = True
    deployment_state["deployment_id"] = str(uuid.uuid4())
    try:
        task = asyncio.create_task(
            deployment_job(
                observation_seconds,
                simulate_failure,
                image_ref,
                registry_auth,
            )
        )
    except Exception:
        deployment_state["active"] = False
        raise
    app.state.deployment_task = task
    return task


@app.post("/api/v1/deploy")
async def start_ci_deployment(
    request: DeployRequest,
    authorization: str | None = Header(default=None),
):
    expected_token = os.getenv("ZERODOWN_DEPLOY_TOKEN")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="CI deployment trigger is not configured",
        )
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8"),
        f"Bearer {expected_token}".encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="Invalid deployment token")

    if (
        request.image is None
        or not re.fullmatch(
            r"ghcr\.io/[a-z0-9][a-z0-9._/-]*:[0-9a-f]{40}",
            request.image,
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="image must be an immutable GHCR image tagged with a commit SHA",
        )
    if not request.registry_username or not request.registry_token:
        raise HTTPException(
            status_code=422,
            detail="GHCR pull credentials are required",
        )

    observation_seconds = (
        request.observation_seconds
        if request.observation_seconds is not None
        else app.state.zerodown_config.rollout.observation_seconds
    )
    schedule_deployment(
        observation_seconds,
        image_ref=request.image,
        registry_auth={
            "username": request.registry_username,
            "password": request.registry_token,
        },
    )
    return {
        "accepted": True,
        "deployment_id": deployment_state["deployment_id"],
        "message": "Deployment started. Check /deployment/status.",
    }


@app.post("/deploy")
async def start_deployment(request: DeployRequest):
    observation_seconds = (
        request.observation_seconds
        if request.observation_seconds is not None
        else app.state.zerodown_config.rollout.observation_seconds
    )

    schedule_deployment(observation_seconds)

    return {
        "accepted": True,
        "deployment_id": deployment_state["deployment_id"],
        "message": "Deployment started. Check /deployment/status.",
    }


@app.post("/demo/failure")
async def start_failure_demo(request: DeployRequest | None = None):
    """Run a canary, fail V2 during observation, and demonstrate rollback."""
    observation_seconds = (
        request.observation_seconds
        if request is not None
        and request.observation_seconds is not None
        else app.state.zerodown_config.rollout.observation_seconds
    )
    schedule_deployment(observation_seconds, True)
    return {
        "accepted": True,
        "message": "Failure demo started. V2 will fail during observation; check /status.",
    }


@app.post("/deployment/cancel")
async def cancel_deployment():
    task = app.state.deployment_task
    if not deployment_state["active"] or task is None:
        raise HTTPException(status_code=409, detail="No deployment is running")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {
        "status": deployment_state["status"],
        "message": deployment_state["message"],
    }


# --------------------------------------------------
# MANUAL ROLLBACK
# --------------------------------------------------

@app.post("/rollback")
async def manual_rollback():

    try:
        task = app.state.deployment_task
        if deployment_state["active"] and task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return {
                "status": "interrupted",
                "v1": 100,
                "v2": 0,
            }
        rollback()

        return {
            "status": "rolled_back",
            "v1": 100,
            "v2": 0,
        }

    except Exception as error:
        record(
            f"Manual rollback failed: {error}",
            "failed",
            DeploymentPhase.FAILED,
        )
        raise HTTPException(
            status_code=500,
            detail=str(error),
        )
