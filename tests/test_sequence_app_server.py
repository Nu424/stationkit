"""Sequence Web App server package tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.sequence_app.server import create_sequence_app_server
from stationkit.testing import MockStationController


def test_sequence_app_server_is_importable_and_serves_api() -> None:
    """Packaged app wrapper can build the sequence API server."""
    client = TestClient(create_sequence_app_server(MockStationController()))

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["controller"]["controller_state"] == "DISCONNECTED"


def test_sequence_app_server_serves_packaged_frontend() -> None:
    """Packaged frontend assets are served when no dist dir is specified."""
    client = TestClient(create_sequence_app_server(MockStationController()))

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
