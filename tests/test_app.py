import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "app")
)

os.environ["APP_VERSION"] = "test"

from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready():
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_api_data():
    response = client.get("/api/data")

    assert response.status_code == 200
    assert response.json()["version"] == "test"
    assert response.json()["message"]