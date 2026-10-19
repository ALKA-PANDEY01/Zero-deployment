
import os

from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Zero-Downtime Deployment Demo",
    description="Demo application for canary releases and automated rollback",
    version="1.0.0",
)

APP_VERSION = os.getenv("APP_VERSION", "v1")
FAIL_HEALTH = os.getenv("FAIL_HEALTH", "false").lower() == "true"
FAIL_SMOKE = os.getenv("FAIL_SMOKE", "false").lower() == "true"


@app.get("/")
def home():
    return {
        "message": "Zero-Downtime Deployment Demo",
        "version": APP_VERSION,
    }


@app.get("/health")
def health():
    if FAIL_HEALTH:
        raise HTTPException(
            status_code=503,
            detail="Simulated unhealthy application",
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
    }


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
