import sys
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import controller.main as controller
from controller.zerodown.router import NginxTrafficRouter


class FakeContainer:
    def __init__(self, name):
        self.name = name
        self.status = "running"
        self.attrs = {"State": {"Health": {"Status": "healthy"}}}
        self.commands = []

    def reload(self):
        return None

    def exec_run(self, command):
        self.commands.append(command)
        return type("Result", (), {"exit_code": 0, "output": b"ok"})()


class FakeContainers:
    def __init__(self):
        self.items = {
            name: FakeContainer(name)
            for name in (
                "zerodown-proxy",
                "zerodown-app-v1",
                "zerodown-app-v2",
            )
        }

    def get(self, name):
        try:
            return self.items[name]
        except KeyError as error:
            raise docker.errors.NotFound(name) from error


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()


@pytest.fixture
def traffic_router(tmp_path):
    return NginxTrafficRouter(
        config_path=tmp_path / "nginx.conf",
        proxy_container="zerodown-proxy",
        v1_target="app_v1",
        v2_target="app_v2",
        v1_container="zerodown-app-v1",
        v2_container="zerodown-app-v2",
        app_port=8000,
        client=FakeDockerClient(),
    )


@pytest.mark.parametrize(
    ("v1_percent", "v2_percent"),
    [(100, 0), (90, 10), (75, 25), (50, 50), (0, 100)],
)
def test_router_applies_supported_percentages(
    traffic_router,
    v1_percent,
    v2_percent,
):
    state = traffic_router.set_percentages(v1_percent, v2_percent)
    nginx_config = traffic_router.config_path.read_text(encoding="utf-8")

    assert state == {
        "v1_percent": v1_percent,
        "v2_percent": v2_percent,
    }
    assert traffic_router.get_traffic() == state
    if v1_percent:
        assert f"server app_v1:8000 weight={v1_percent}" in nginx_config
    else:
        assert "server app_v1:" not in nginx_config
    if v2_percent:
        assert f"server app_v2:8000 weight={v2_percent}" in nginx_config
    else:
        assert "server app_v2:" not in nginx_config
    assert traffic_router.client.containers.items["zerodown-proxy"].commands == [
        "nginx -t",
        "nginx -s reload",
    ]


@pytest.mark.parametrize(("v1_percent", "v2_percent"), [(90, 20), (-1, 101), (1.5, 98.5)])
def test_router_rejects_invalid_percentages(
    traffic_router,
    v1_percent,
    v2_percent,
):
    with pytest.raises(ValueError, match="integers between 0 and 100 and total 100"):
        traffic_router.set_percentages(v1_percent, v2_percent)


def test_router_refuses_to_send_traffic_to_unhealthy_container(traffic_router):
    traffic_router.client.containers.items["zerodown-app-v2"].attrs["State"][
        "Health"
    ]["Status"] = "unhealthy"

    with pytest.raises(ValueError, match="V2 .* unhealthy"):
        traffic_router.set_percentages(90, 10)


def test_router_uses_configured_application_port(tmp_path):
    router = NginxTrafficRouter(
        config_path=tmp_path / "nginx.conf",
        proxy_container="zerodown-proxy",
        v1_target="app_v1",
        v2_target="app_v2",
        v1_container="zerodown-app-v1",
        v2_container="zerodown-app-v2",
        app_port=8123,
        client=FakeDockerClient(),
    )

    router.set_percentages(50, 50)

    nginx_config = router.config_path.read_text(encoding="utf-8")
    assert "server app_v1:8123 weight=50" in nginx_config
    assert "server app_v2:8123 weight=50" in nginx_config


def test_router_restores_traffic_state_from_generated_nginx_config(tmp_path):
    config_path = tmp_path / "nginx.conf"
    router = NginxTrafficRouter(
        config_path=config_path,
        proxy_container="zerodown-proxy",
        v1_target="app_v1",
        v2_target="app_v2",
        v1_container="zerodown-app-v1",
        v2_container="zerodown-app-v2",
        app_port=8000,
        client=FakeDockerClient(),
    )
    router.set_percentages(75, 25)

    restarted_router = NginxTrafficRouter(
        config_path=config_path,
        proxy_container="zerodown-proxy",
        v1_target="app_v1",
        v2_target="app_v2",
        v1_container="zerodown-app-v1",
        v2_container="zerodown-app-v2",
        app_port=8000,
        client=FakeDockerClient(),
    )

    assert restarted_router.get_traffic() == {
        "v1_percent": 75,
        "v2_percent": 25,
    }


def test_router_can_replace_inactive_v1_target_after_v2_promotion(traffic_router):
    traffic_router.set_percentages(0, 100)

    traffic_router.set_version_target(
        "v1",
        "zerodown-app-v1-rollback",
        "zerodown-app-v1-rollback",
    )

    assert traffic_router.targets["v1"] == (
        "zerodown-app-v1-rollback",
        "zerodown-app-v1-rollback",
    )
    with pytest.raises(ValueError, match="Cannot change the V2 target"):
        traffic_router.set_version_target(
            "v2",
            "other-v2",
            "other-v2",
        )


def test_router_restores_promoted_runtime_targets(tmp_path):
    config_path = tmp_path / "nginx.conf"
    config_path.write_text(
        "upstream app_pool {\n"
        "  server promoted-v2:8000 weight=100 max_fails=2 fail_timeout=5s;\n"
        "}\n",
        encoding="utf-8",
    )
    router = NginxTrafficRouter(
        config_path=config_path,
        proxy_container="zerodown-proxy",
        v1_target="app_v1",
        v2_target="app_v2",
        v1_container="zerodown-app-v1",
        v2_container="zerodown-app-v2",
        app_port=8000,
        client=FakeDockerClient(),
    )

    router.restore_targets(
        {
            "v1": ("promoted-rollback", "promoted-rollback"),
            "v2": ("promoted-v2", "promoted-v2"),
        }
    )

    assert router.get_traffic() == {"v1_percent": 0, "v2_percent": 100}


def test_traffic_api_reads_and_updates_router_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        controller.app.state,
        "traffic_router",
        NginxTrafficRouter(
            config_path=tmp_path / "nginx.conf",
            proxy_container="zerodown-proxy",
            v1_target="app_v1",
            v2_target="app_v2",
            v1_container="zerodown-app-v1",
            v2_container="zerodown-app-v2",
            app_port=8000,
            client=FakeDockerClient(),
        ),
    )

    with TestClient(controller.app) as client:
        initial = client.get("/deployment/traffic")
        updated = client.put("/deployment/traffic", json={"v2_percent": 10})
        current = client.get("/deployment/traffic")
        unsupported = client.put(
            "/deployment/traffic",
            json={"v2_percent": 101},
        )

    assert initial.json() == {"v1_percent": 100, "v2_percent": 0}
    assert updated.status_code == 200
    assert updated.json() == {"v1_percent": 90, "v2_percent": 10}
    assert current.json() == updated.json()
    assert unsupported.status_code == 422
