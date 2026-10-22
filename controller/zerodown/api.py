"""REST endpoints exposing the configuration and deployment phase model."""

from dataclasses import asdict

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/v1", tags=["ZeroDown"])


@router.get("/config")
def get_zero_down_config(request: Request):
    config = asdict(request.app.state.zerodown_config)
    config["rollout"]["stages"] = list(config["rollout"]["stages"])
    config["health"]["smoke_endpoints"] = list(config["health"]["smoke_endpoints"])
    return config


@router.get("/deployment/state")
def get_deployment_phase(request: Request):
    return asdict(request.app.state.deployment_state_store.snapshot())
