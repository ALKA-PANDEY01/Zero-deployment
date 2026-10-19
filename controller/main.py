import asyncio
import os
import time
from datetime import datetime, timezone

import docker
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(
    title="Zero-Downtime Deployment Controller",
    version="1.0.0",
)


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

PROXY_CONFIG = os.getenv(
    "PROXY_CONFIG",
    "/shared/nginx.conf"
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

DEFAULT_OBSERVATION_SECONDS = int(
    os.getenv("OBSERVATION_SECONDS", "15")
)


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

def record(message: str, status: str | None = None):

    deployment_state["steps"].append(
        {
            "time": datetime.now(
                timezone.utc
            ).isoformat(),

            "message": message,
        }
    )

    if status:
        deployment_state["status"] = status


# --------------------------------------------------
# CHANGE NGINX TRAFFIC
# --------------------------------------------------

def set_traffic(v1: int, v2: int):

    if v1 + v2 != 100:
        raise ValueError(
            "Traffic percentages must total 100"
        )

    nginx_config = f"""
worker_processes auto;

events {{
    worker_connections 1024;
}}

http {{

    access_log /dev/stdout;
    error_log /dev/stderr warn;

    upstream app_pool {{

        server app_v1:8000
            weight={v1}
            max_fails=2
            fail_timeout=5s;

        server app_v2:8000
            weight={v2}
            max_fails=2
            fail_timeout=5s;
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

    with open(PROXY_CONFIG, "w") as file:
        file.write(nginx_config)

    docker_client = docker.from_env()

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

    tests = [
        "/health",
        "/ready",
        "/api/data",
    ]

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


# --------------------------------------------------
# ROLLBACK
# --------------------------------------------------

def rollback():

    record(
        "Starting automatic rollback",
        "rolling_back",
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
            # STEP 2
            # 10% CANARY
            # --------------------------------------

            set_traffic(
                90,
                10,
            )

            await asyncio.sleep(2)

            record(
                "10% canary period passed"
            )

            # --------------------------------------
            # STEP 3
            # 25% CANARY
            # --------------------------------------

            set_traffic(
                75,
                25,
            )

            await asyncio.sleep(2)

            record(
                "25% canary period passed"
            )

            # --------------------------------------
            # STEP 4
            # 50% CANARY
            # --------------------------------------

            set_traffic(
                50,
                50,
            )

            await asyncio.sleep(2)

            record(
                "50% canary period passed"
            )

            # --------------------------------------
            # STEP 5
            # 75% CANARY
            # --------------------------------------

            set_traffic(
                25,
                75,
            )

            await asyncio.sleep(2)

            record(
                "75% canary period passed"
            )

            # --------------------------------------
            # OBSERVATION
            # --------------------------------------

            record(
                f"Observing V2 for "
                f"{observation_seconds} seconds"
            )

            deadline = (
                time.monotonic()
                + observation_seconds
            )
            failure_injected = False

            while (
                time.monotonic()
                < deadline
            ):

                if (
                    simulate_failure
                    and not failure_injected
                    and observation_seconds > 2
                    and deadline - time.monotonic()
                    <= observation_seconds - 2
                ):
                    await set_v2_demo_failure(True)
                    failure_injected = True
                    record("Demo injected V2 health failure")

                success, message = (
                    await check_endpoint(
                        V2_URL,
                        "/health",
                        "v2",
                    )
                )

                if not success:

                    record(
                        f"Observation failed: {message}"
                    )

                    rollback()

                    if simulate_failure:
                        await set_v2_demo_failure(False)
                        record("V2 demo health restored after rollback")

                    return

                await asyncio.sleep(1)

            # --------------------------------------
            # FINAL PROMOTION
            # --------------------------------------

            set_traffic(
                0,
                100,
            )

            record(
                "V2 promoted to 100% traffic",
                "succeeded",
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
                )

        finally:

            deployment_state["finished_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )


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
        else DEFAULT_OBSERVATION_SECONDS
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
        else DEFAULT_OBSERVATION_SECONDS
    )
    if not 3 <= observation_seconds <= 300:
        raise HTTPException(
            status_code=400,
            detail="observation_seconds must be between 3 and 300 for the failure demo",
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
