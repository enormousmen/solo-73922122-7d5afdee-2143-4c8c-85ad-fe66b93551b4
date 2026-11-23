"""API-level tests: request validation and end-to-end probe via TestClient."""
from __future__ import annotations

import importlib
import os

from fake_receiver import FakeReceiver


def _make_client(receiver_port: int):
    os.environ["RECEIVER_HOST"] = "127.0.0.1"
    os.environ["RECEIVER_PORT"] = str(receiver_port)
    from prober.app import main

    importlib.reload(main)  # pick up the env-configured receiver
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def test_probe_endpoint_end_to_end():
    with FakeReceiver(drop_threshold=1400) as rx:
        client = _make_client(rx.port)
        resp = client.post("/probe", json={"timeout_ms": 150})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "ok"
    assert body["max_confirmed_size"] <= 1400 < body["min_suspected_oversize"]
    assert body["boundary_gap"] <= 8
    assert body["packets_sent"] <= 48
    assert body["groups"] and body["packets"]


def test_probe_endpoint_no_loss():
    with FakeReceiver() as rx:
        client = _make_client(rx.port)
        resp = client.post("/probe", json={"timeout_ms": 150})
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_confirmed_size"] == 1472
    assert body["min_suspected_oversize"] is None


def test_probe_request_validation():
    with FakeReceiver() as rx:
        client = _make_client(rx.port)
        assert client.post("/probe", json={"budget": 100}).status_code == 422
        assert client.post("/probe", json={"max_size": 2000}).status_code == 422
        assert client.post("/probe", json={"min_size": 32}).status_code == 422
        assert (
            client.post("/probe", json={"min_size": 1000, "max_size": 500}).status_code
            == 422
        )
        assert client.get("/healthz").status_code == 200
