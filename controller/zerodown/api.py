"""REST endpoints exposing the configuration and deployment phase model."""

from dataclasses import asdict

import docker
from fastapi import APIRouter, HTTPException, Query, Request


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


def _version_details(request: Request, version: str):
    manager = request.app.state.docker_manager
    try:
        manager.validate_version(version)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    app_config = request.app.state.zerodown_config.app
    return (
        manager,
        app_config,
        manager.container_name(app_config.name, version),
        manager.image_tag(app_config.name, version),
        manager.host_port(version),
    )


def _docker_error(error: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Docker operation failed: {error}")


@router.post("/versions/{version}/build")
def build_version(version: str, request: Request):
    manager, app_config, container_name, image_tag, _ = _version_details(
        request,
        version,
    )
    try:
        if manager.container_exists(container_name):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Container '{container_name}' already exists. "
                    "Remove it before building a replacement image."
                ),
            )
        image_id = manager.build_image(
            app_config.build_context,
            app_config.dockerfile,
            image_tag,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (docker.errors.DockerException, RuntimeError) as error:
        raise _docker_error(error) from error
    return {
        "version": version,
        "image": image_tag,
        "image_id": image_id,
        "status": "built",
    }


@router.post("/versions/{version}/start")
def start_version(version: str, request: Request):
    manager, app_config, container_name, image_tag, host_port = _version_details(
        request,
        version,
    )
    try:
        details = manager.start_container(
            image=image_tag,
            name=container_name,
            port=app_config.port,
            version=version,
            app_name=app_config.name,
            host_port=host_port,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise _docker_error(error) from error
    return {
        **details,
        "version": version,
        "url": f"http://localhost:{host_port}",
    }


@router.post("/versions/{version}/validate")
async def validate_version(version: str, request: Request):
    manager = request.app.state.docker_manager
    try:
        manager.validate_version(version)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    validation_result = await request.app.state.version_health_checker.validate_version(
        version,
    )
    return validation_result.to_dict()


@router.post("/versions/{version}/stop")
def stop_version(version: str, request: Request):
    manager, _, container_name, _, _ = _version_details(request, version)
    try:
        return manager.stop_container(container_name)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise _docker_error(error) from error


@router.delete("/versions/{version}")
def remove_version(version: str, request: Request):
    manager, _, container_name, _, _ = _version_details(request, version)
    traffic_router = request.app.state.traffic_router
    if (
        version == "v2"
        and traffic_router.targets["v2"][1] == container_name
        and traffic_router.get_traffic()["v2_percent"] != 0
    ):
        raise HTTPException(
            status_code=409,
            detail="Cannot remove V2 while it is receiving traffic",
        )
    try:
        if (
            version == "v2"
            and traffic_router.targets["v2"][1] == container_name
        ):
            traffic_router.reset_version_target("v2")
        manager.remove_container(container_name)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise _docker_error(error) from error
    return {"version": version, "container": container_name, "status": "removed"}


@router.get("/versions/{version}")
def inspect_version(version: str, request: Request):
    manager, _, container_name, _, host_port = _version_details(
        request,
        version,
    )
    try:
        details = manager.inspect_container(container_name)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise _docker_error(error) from error
    return {
        **details,
        "version": version,
        "url": f"http://localhost:{host_port}",
    }


@router.get("/versions/{version}/logs")
def get_version_logs(
    version: str,
    request: Request,
    tail: int = Query(default=100, ge=1, le=1000),
):
    manager, _, container_name, _, _ = _version_details(request, version)
    try:
        logs = manager.container_logs(container_name, tail=tail)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise _docker_error(error) from error
    return {"version": version, "logs": logs}
