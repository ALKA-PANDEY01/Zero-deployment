import os
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "app")
)

os.environ["APP_VERSION"] = "test"

from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_each_version_health_is_200_when_healthy(monkeypatch, version):
    monkeypatch.setattr(main, "APP_VERSION", version)
    monkeypatch.setattr(main, "FAIL_HEALTH", False)
    monkeypatch.setattr(main, "demo_health_failure", False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == version


def test_ready():
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_version_endpoint_returns_running_version(monkeypatch, version):
    monkeypatch.setattr(main, "APP_VERSION", version)

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": version}


def test_api_data():
    response = client.get("/api/data")

    assert response.status_code == 200
    assert response.json()["version"] == "test"
    assert response.json()["message"]
