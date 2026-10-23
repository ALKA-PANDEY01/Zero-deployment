
import hmac
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Zero-Downtime Deployment Demo",
    description="Demo application for canary releases and automated rollback",
    version="1.0.0",
)

APP_VERSION = os.getenv("APP_VERSION", "v1")


logger = logging.getLogger("zerodown.application")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    details = [
        {key: item[key] for key in ("loc", "msg", "type") if key in item}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception):
    logger.error(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "ERROR",
        "message": "Unhandled application request error",
        "path": request.url.path,
        "error_type": type(error).__name__,
    }))
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred"},
    )


def env_bool(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be a boolean value")
    return value in {"true", "1", "yes", "on"}


def env_non_negative_int(name: str) -> int:
    value = os.getenv(name, "0")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


FAIL_HEALTH = env_bool("FAIL_HEALTH")
FAIL_SMOKE = env_bool("FAIL_SMOKE")
DEMO_FAILURE_AFTER_REQUESTS = env_non_negative_int(
    "ZERODOWN_DEMO_FAILURE_AFTER_REQUESTS"
)
DEMO_CONTROL_TOKEN = os.getenv("APP_DEMO_CONTROL_TOKEN")
demo_home_requests = 0
demo_health_failure = False


@app.get("/")
def home():
    global demo_home_requests
    if APP_VERSION == "v2" and DEMO_FAILURE_AFTER_REQUESTS > 0:
        demo_home_requests += 1
        if demo_home_requests > DEMO_FAILURE_AFTER_REQUESTS:
            raise HTTPException(
                status_code=500,
                detail="Deterministic ZeroDown rollback demonstration failure",
            )
    return {
        "message": "Zero-Downtime Deployment Demo",
        "version": APP_VERSION,
    }


@app.get("/health")
def health():
    if demo_health_failure:
        raise HTTPException(
            status_code=500,
            detail="Simulated V2 health check failure",
        )

    if FAIL_HEALTH:
        raise HTTPException(
            status_code=503,
            detail="Simulated unhealthy application",
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
    }


@app.get("/version")
def version():
    return {"version": APP_VERSION}


@app.get("/ready")
def readiness():
    return {
        "status": "ready",
        "version": APP_VERSION,
    }


@app.get("/api/data")
def get_data():
    if FAIL_SMOKE:
        raise HTTPException(
            status_code=500,
            detail="Simulated application failure",
        )

    return {
        "message": "Application is working correctly",
        "version": APP_VERSION,
        "deployment": "zero-downtime-demo",
    }


@app.post("/demo/failure/{enabled}")
def set_demo_failure(enabled: bool, authorization: str | None = Header(default=None)):
    if not DEMO_CONTROL_TOKEN or not authorization or not hmac.compare_digest(
        authorization.encode("utf-8"),
        f"Bearer {DEMO_CONTROL_TOKEN}".encode("utf-8"),
    ):
        raise HTTPException(status_code=404, detail="Not found")
    global demo_health_failure
    demo_health_failure = enabled
    return {"health_failure_enabled": demo_health_failure}
