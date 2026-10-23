"""Build and manage isolated application-version containers with Docker."""

import re
from pathlib import Path
from typing import Any

import docker
from docker.errors import ImageNotFound, NotFound


class DockerManager:
    def __init__(
        self,
        project_root: Path = Path("/workspace"),
        network: str = "zerodown",
        host_port_base: int = 18100,
        client: Any | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.network = network
        self.host_port_base = host_port_base
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env(timeout=120)
        return self._client

    @staticmethod
    def validate_version(version: str) -> str:
        if version not in {"v1", "v2"}:
            raise ValueError("version must be either 'v1' or 'v2'")
        return version

    @staticmethod
    def _app_slug(app_name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")
        if not slug:
            raise ValueError("app.name must contain letters or numbers for Docker names")
        return slug

    def container_name(self, app_name: str, version: str) -> str:
        self.validate_version(version)
        return f"zerodown-{self._app_slug(app_name)}-{version}"

    def image_tag(self, app_name: str, version: str) -> str:
        self.validate_version(version)
        return f"zerodown/{self._app_slug(app_name)}:{version}"

    def host_port(self, version: str) -> int:
        self.validate_version(version)
        return self.host_port_base + int(version[1:])

    def app_url(self, app_name: str, version: str, port: int) -> str:
        return (
            f"http://{self.container_name(app_name, version)}:"
            f"{port}"
        )

    def _container(self, name: str):
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def container_exists(self, name: str) -> bool:
        return self._container(name) is not None

    def pull_image(
        self,
        image: str,
        auth_config: dict[str, str] | None = None,
    ) -> str:
        try:
            pulled = self.client.images.pull(image, auth_config=auth_config)
        except docker.errors.DockerException as error:
            raise RuntimeError(f"Docker image pull failed for '{image}'") from error
        return pulled.id

    def build_image(self, build_context: str, dockerfile: str, tag: str) -> str:
        context_path = Path(build_context)
        if not context_path.is_absolute():
            context_path = self.project_root / context_path
        context_path = context_path.resolve()
        if not context_path.is_relative_to(self.project_root):
            raise ValueError("app.build_context must be inside the mounted project directory")
        if not context_path.is_dir():
            raise ValueError(f"Build context does not exist or is not a directory: {context_path}")

        dockerfile_path = Path(dockerfile)
        if dockerfile_path.is_absolute():
            raise ValueError("app.dockerfile must be relative to app.build_context")
        resolved_dockerfile = (context_path / dockerfile_path).resolve()
        if (
            not resolved_dockerfile.is_relative_to(context_path)
            or not resolved_dockerfile.is_file()
        ):
            raise ValueError(
                f"Dockerfile does not exist inside the build context: {dockerfile}"
            )

        try:
            image, _ = self.client.images.build(
                path=str(context_path),
                dockerfile=dockerfile_path.as_posix(),
                tag=tag,
                rm=True,
            )
        except docker.errors.BuildError as error:
            details = error.msg or str(error)
            raise RuntimeError(f"Docker image build failed: {details}") from error
        return image.id

    def start_container(
        self,
        image: str,
        name: str,
        port: int,
        version: str,
        app_name: str,
        host_port: int | None,
        extra_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.validate_version(version)
        existing = self._container(name)
        if existing is not None:
            existing.reload()
            if existing.status == "running":
                return self.inspect_container(name)
            existing.start()
            return self.inspect_container(name)

        try:
            container = self.client.containers.run(
                image=image,
                name=name,
                detach=True,
                network=self.network,
                ports=(
                    {f"{port}/tcp": ("127.0.0.1", host_port)}
                    if host_port is not None
                    else None
                ),
                environment={
                    "APP_VERSION": version,
                    "APP_PORT": str(port),
                    **(extra_environment or {}),
                },
                labels={
                    "com.zerodown.app": app_name,
                    "com.zerodown.version": version,
                },
            )
        except ImageNotFound as error:
            raise ValueError(f"Image '{image}' has not been built") from error
        return self.inspect_container(container.name)

    def stop_container(self, name: str) -> dict[str, Any]:
        container = self._container(name)
        if container is None:
            raise ValueError(f"Container '{name}' was not found")
        container.reload()
        if container.status == "running":
            container.stop(timeout=10)
        return self.inspect_container(name)

    def remove_container(self, name: str) -> None:
        container = self._container(name)
        if container is None:
            raise ValueError(f"Container '{name}' was not found")
        container.remove(force=True)

    def inspect_container(self, name: str) -> dict[str, Any]:
        container = self._container(name)
        if container is None:
            raise ValueError(f"Container '{name}' was not found")
        container.reload()
        state = container.attrs.get("State", {})
        health = state.get("Health", {}).get("Status", "not_configured")
        return {
            "id": container.id,
            "name": container.name,
            "image": container.image.tags[0] if container.image.tags else container.image.id,
            "status": container.status,
            "health": health,
            "ports": container.attrs.get("NetworkSettings", {}).get("Ports", {}),
        }

    def container_logs(self, name: str, tail: int = 100) -> str:
        container = self._container(name)
        if container is None:
            raise ValueError(f"Container '{name}' was not found")
        return container.logs(tail=tail, stdout=True, stderr=True).decode(
            "utf-8",
            errors="replace",
        )
