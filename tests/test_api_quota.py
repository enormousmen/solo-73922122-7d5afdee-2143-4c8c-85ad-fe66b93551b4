"""API tests for the in-flight quota: endpoints, env validation, parallel runs."""
from __future__ import annotations

import importlib
import os
import threading
import time

import httpx
import uvicorn

from fake_receiver import FakeReceiver


def _make_client(receiver_port: int):
    os.environ["RECEIVER_HOST"] = "127.0.0.1"
    os.environ["RECEIVER_PORT"] = str(receiver_port)
    from prober.app import main

    importlib.reload(main)  # pick up the env-configured receiver and quota
    from fastapi.testclient import TestClient

    return main, TestClient(main.app)


def test_healthz_and_quota_endpoints():
    with FakeReceiver() as rx:
        main, client = _make_client(rx.port)
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["PROBE_INFLIGHT_QUOTA_BYTES"] == main.QUOTA.limit

        snap = client.get("/quota").json()
        assert snap["limit_bytes"] == main.QUOTA.limit
        assert snap["inflight_bytes"] == 0
        assert snap["available_bytes"] == snap["limit_bytes"]


def test_quota_env_is_validated_at_import(monkeypatch):
    monkeypatch.setenv("PROBE_INFLIGHT_QUOTA_BYTES", "1024")  # below 1536
    with FakeReceiver() as rx:
        os.environ["RECEIVER_PORT"] = str(rx.port)
        from prober.app import main

        try:
            importlib.reload(main)
        except ValueError as exc:
            assert "minimum" in str(exc)
        else:  # pragma: no cover - reload must raise
            raise AssertionError("expected ValueError for quota below minimum")
    monkeypatch.delenv("PROBE_INFLIGHT_QUOTA_BYTES")


def test_parallel_probe_requests_share_process_quota():
    # Real uvicorn server (threadpool executes concurrent /probe calls) plus
    # two parallel HTTP clients, demonstrating one round waiting while another
    # sends with bytes the first one just released.
    with FakeReceiver() as rx:
        os.environ["RECEIVER_HOST"] = "127.0.0.1"
        os.environ["RECEIVER_PORT"] = str(rx.port)
        os.environ["PROBE_INFLIGHT_QUOTA_BYTES"] = "1536"
        from prober.app import main

        importlib.reload(main)
        config = uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.01)
        bound_port = server.servers[0].sockets[0].getsockname()[1]

        responses: dict[str, dict] = {}
        errors: dict[str, Exception] = {}

        def call(name: str, delay: float):
            time.sleep(delay)  # stagger so A reserves the quota first
            try:
                resp = httpx.post(
                    f"http://127.0.0.1:{bound_port}/probe",
                    json={"timeout_ms": 400, "final_wait_ms": 200},
                    timeout=15,
                )
                responses[name] = resp.json()
            except Exception as exc:  # pragma: no cover - failure path
                errors[name] = exc

        t1 = threading.Thread(target=call, args=("a", 0.0))
        t2 = threading.Thread(target=call, args=("b", 0.08))
        t1.start()
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)
        server.should_exit = True
        thread.join(timeout=5)

    assert not errors, errors
    assert responses["a"]["verdict"] == "ok"
    assert responses["b"]["verdict"] == "ok"
    # B's first group queued behind A's in-flight 1536 bytes.
    assert responses["b"]["groups"][0]["quota_wait_ms"] > 0
    assert responses["b"]["inflight_quota"]["limit_bytes"] == 1536
    # Both runs returned everything they reserved.
    assert main.QUOTA.snapshot()["inflight_bytes"] == 0
