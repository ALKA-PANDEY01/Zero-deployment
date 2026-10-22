import sys
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller
from controller.zerodown.docker_manager import DockerManager


class FakeImage:
    id = "sha256:demo-image"
    tags = ["zerodown/demo-app:v1"]


class FakeContainer:
    def __init__(self, name, image, network, ports, environment, labels):
        self.id = f"id-{name}"
        self.name = name
        self.image = image
        self.status = "running"
        self.attrs = {
            "State": {"Health": {"Status": "healthy"}},
            "NetworkSettings": {
                "Ports": {f"{ports}/tcp": [{"HostPort": "18101"}]},
            },
        }
        self.logs_output = b"demo application started\n"
        self.environment = environment
        self.labels = labels
        self.network = network

    def reload(self):
        return None

    def start(self):
        self.status = "running"

    def stop(self, timeout):
        self.status = "exited"

    def remove(self, force):
        del self.client.containers.items[self.name]

    def logs(self, **kwargs):
        return self.logs_output


class FakeContainers:
    def __init__(self):
        self.items = {}

    def get(self, name):
        try:
            return self.items[name]
        except KeyError as error:
            raise docker.errors.NotFound("container not found") from error

    def run(self, image, name, detach, network, ports, environment, labels):
        container_port, host = next(iter(ports.items()))
        container = FakeContainer(
            name,
            FakeImage(),
            network,
            int(container_port.split("/")[0]),
            environment,
            labels,
        )
        container.client = type("ContainerClient", (), {"containers": self})()
        container.attrs["NetworkSettings"]["Ports"][container_port][0][
            "HostPort"
        ] = str(host[1])
        self.items[name] = container
        return container


class FakeImages:
    def __init__(self):
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return FakeImage(), []


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.images = FakeImages()


def test_manager_builds_within_project_and_generates_unique_version_names(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    client = FakeDockerClient()
    manager = DockerManager(project_root=tmp_path, client=client)

    image_id = manager.build_image("./app", "Dockerfile", "zerodown/demo-app:v1")

    assert image_id == FakeImage.id
    assert client.images.calls == [
        {
            "path": str(app_dir),
            "dockerfile": "Dockerfile",
            "tag": "zerodown/demo-app:v1",
            "rm": True,
        },
    ]
    assert manager.container_name("Demo App", "v1") == "zerodown-demo-app-v1"
    assert manager.container_name("Demo App", "v2") == "zerodown-demo-app-v2"
    assert manager.image_tag("Demo App", "v1") != manager.image_tag("Demo App", "v2")


def test_manager_rejects_build_context_outside_project_root(tmp_path):
    manager = DockerManager(project_root=tmp_path, client=FakeDockerClient())

    with pytest.raises(ValueError, match="inside the mounted project directory"):
        manager.build_image("..", "Dockerfile", "demo:v1")


def test_manager_keeps_versions_independent_and_reports_health_and_logs():
    client = FakeDockerClient()
    manager = DockerManager(client=client)

    v1 = manager.start_container(
        image="zerodown/demo-app:v1",
        name="zerodown-demo-app-v1",
        port=8000,
        version="v1",
        app_name="demo-app",
        host_port=18101,
    )
    v2 = manager.start_container(
        image="zerodown/demo-app:v2",
        name="zerodown-demo-app-v2",
        port=8000,
        version="v2",
        app_name="demo-app",
        host_port=18102,
    )

    assert v1["status"] == v2["status"] == "running"
    assert v1["health"] == v2["health"] == "healthy"
    assert manager.inspect_container("zerodown-demo-app-v1")["status"] == "running"
    assert manager.container_logs("zerodown-demo-app-v2") == "demo application started\n"

    manager.stop_container("zerodown-demo-app-v1")

    assert manager.inspect_container("zerodown-demo-app-v1")["status"] == "exited"
    assert manager.inspect_container("zerodown-demo-app-v2")["status"] == "running"
    manager.remove_container("zerodown-demo-app-v1")
    assert not manager.container_exists("zerodown-demo-app-v1")


def test_version_api_build_start_inspect_logs_stop_and_remove(monkeypatch):
    manager = DockerManager(
        project_root=Path(__file__).resolve().parents[1],
        client=FakeDockerClient(),
    )
    monkeypatch.setattr(controller.app.state, "docker_manager", manager)

    with TestClient(controller.app) as client:
        built = client.post("/api/v1/versions/v1/build")
        started = client.post("/api/v1/versions/v1/start")
        inspected = client.get("/api/v1/versions/v1")
        logs = client.get("/api/v1/versions/v1/logs")
        stopped = client.post("/api/v1/versions/v1/stop")
        removed = client.delete("/api/v1/versions/v1")
        invalid = client.post("/api/v1/versions/v3/start")

    assert built.status_code == 200
    assert built.json()["status"] == "built"
    assert started.status_code == 200
    assert started.json()["url"] == "http://localhost:18101"
    assert inspected.json()["health"] == "healthy"
    assert logs.json()["logs"] == "demo application started\n"
    assert stopped.json()["status"] == "exited"
    assert removed.json()["status"] == "removed"
    assert invalid.status_code == 400
