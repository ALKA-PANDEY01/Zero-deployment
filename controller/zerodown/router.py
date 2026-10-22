"""Programmatically route requests between the stable and candidate versions."""

import re
from pathlib import Path
from threading import Lock
from typing import Any

import docker
from docker.errors import NotFound
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


class NginxTrafficRouter:
    def __init__(
        self,
        config_path: Path,
        proxy_container: str,
        v1_target: str,
        v2_target: str,
        v1_container: str,
        v2_container: str,
        app_port: int,
        client: Any | None = None,
    ) -> None:
        self.config_path = config_path
        self.proxy_container = proxy_container
        self.targets = {
            "v1": (v1_target, v1_container),
            "v2": (v2_target, v2_container),
        }
        self._default_targets = dict(self.targets)
        self.app_port = app_port
        self._client = client
        self._lock = Lock()
        self._traffic = self._read_configured_traffic()

    def _read_configured_traffic(self) -> dict[str, int]:
        try:
            config = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"v1_percent": 100, "v2_percent": 0}

        traffic = {"v1_percent": 0, "v2_percent": 0}
        for version, (target, _) in self.targets.items():
            match = re.search(
                rf"\bserver\s+{re.escape(target)}:{self.app_port}\s+weight=(\d+)\b",
                config,
            )
            if match:
                traffic[f"{version}_percent"] = int(match.group(1))
        if sum(traffic.values()) == 100:
            return traffic
        return {"v1_percent": 100, "v2_percent": 0}

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env(timeout=3)
        return self._client

    def get_traffic(self) -> dict[str, int]:
        with self._lock:
            return dict(self._traffic)

    def set_version_target(
        self,
        version: str,
        target: str,
        container_name: str,
    ) -> None:
        if version not in self.targets:
            raise ValueError("version must be either 'v1' or 'v2'")
        if not target or not container_name:
            raise ValueError("target and container_name must be non-empty")
        with self._lock:
            if self._traffic["v2_percent"] != 0:
                raise ValueError("Reset traffic to V1 before changing a target")
            self.targets[version] = (target, container_name)

    def reset_version_target(self, version: str) -> None:
        if version not in self._default_targets:
            raise ValueError("version must be either 'v1' or 'v2'")
        with self._lock:
            if version == "v2" and self._traffic["v2_percent"] != 0:
                raise ValueError("Reset traffic to V1 before removing its target")
            self.targets[version] = self._default_targets[version]

    def _ensure_ready(self, version: str) -> None:
        _, container_name = self.targets[version]
        try:
            container = self.client.containers.get(container_name)
            container.reload()
        except NotFound as error:
            raise ValueError(
                f"{version.upper()} container '{container_name}' is not available"
            ) from error

        if container.status != "running":
            raise ValueError(
                f"{version.upper()} container '{container_name}' is not running"
            )
        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        if health in {"starting", "unhealthy"}:
            raise ValueError(
                f"{version.upper()} container '{container_name}' is {health}"
            )

    def _render_config(self, v1_percent: int, v2_percent: int) -> str:
        servers = []
        for version, percentage in (
            ("v1", v1_percent),
            ("v2", v2_percent),
        ):
            if percentage:
                target, _ = self.targets[version]
                servers.append(
                    f"        server {target}:{self.app_port} "
                    f"weight={percentage} max_fails=2 fail_timeout=5s;"
                )
        upstream = "\n".join(servers)
        return f"""worker_processes auto;

events {{
    worker_connections 1024;
}}

http {{
    access_log /dev/stdout;
    error_log /dev/stderr warn;

    upstream app_pool {{
{upstream}
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

    def set_percentages(self, v1_percent: int, v2_percent: int) -> dict[str, int]:
        if (
            isinstance(v1_percent, bool)
            or isinstance(v2_percent, bool)
            or not isinstance(v1_percent, int)
            or not isinstance(v2_percent, int)
            or not 0 <= v1_percent <= 100
            or not 0 <= v2_percent <= 100
            or v1_percent + v2_percent != 100
        ):
            raise ValueError(
                "Traffic percentages must be integers between 0 and 100 and total 100"
            )

        with self._lock:
            if v1_percent:
                self._ensure_ready("v1")
            if v2_percent:
                self._ensure_ready("v2")

            try:
                previous_config = self.config_path.read_text(encoding="utf-8")
            except OSError:
                previous_config = None

            proxy = self.client.containers.get(self.proxy_container)
            try:
                self.config_path.write_text(
                    self._render_config(v1_percent, v2_percent),
                    encoding="utf-8",
                )
                test_result = proxy.exec_run("nginx -t")
                if test_result.exit_code != 0:
                    output = test_result.output.decode(errors="replace")
                    raise RuntimeError(f"Nginx configuration failed: {output}")

                reload_result = proxy.exec_run("nginx -s reload")
                if reload_result.exit_code != 0:
                    output = reload_result.output.decode(errors="replace")
                    raise RuntimeError(f"Nginx reload failed: {output}")
            except (docker.errors.DockerException, RuntimeError, OSError) as error:
                if previous_config is not None:
                    self.config_path.write_text(previous_config, encoding="utf-8")
                    restore_result = proxy.exec_run("nginx -s reload")
                    if restore_result.exit_code != 0:
                        output = restore_result.output.decode(errors="replace")
                        raise RuntimeError(
                            f"Traffic update failed and previous Nginx config "
                            f"could not be restored: {output}"
                        ) from error
                raise

            self._traffic = {
                "v1_percent": v1_percent,
                "v2_percent": v2_percent,
            }
            return dict(self._traffic)


traffic_api = APIRouter(tags=["Traffic"])


class TrafficUpdate(BaseModel):
    v2_percent: int = Field(ge=0, le=100)


@traffic_api.get("/deployment/traffic")
def get_traffic(request: Request):
    return request.app.state.traffic_router.get_traffic()


@traffic_api.put("/deployment/traffic")
def update_traffic(update: TrafficUpdate, request: Request):
    router = request.app.state.traffic_router
    try:
        return router.set_percentages(
            100 - update.v2_percent,
            update.v2_percent,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise HTTPException(
            status_code=502,
            detail=f"Docker traffic-routing operation failed: {error}",
        ) from error
