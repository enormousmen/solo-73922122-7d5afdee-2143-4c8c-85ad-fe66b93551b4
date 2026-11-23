"""Engine tests for the process-wide in-flight byte quota."""
from __future__ import annotations

import socket
import threading
import time

from fake_receiver import FakeReceiver
from prober.app.engine import ProbeEngine
from prober.app.quota import MIN_QUOTA_BYTES, ByteQuota


def make_quota(limit: int = MIN_QUOTA_BYTES) -> ByteQuota:
    return ByteQuota(limit, log=lambda msg: None)


def run(receiver: FakeReceiver, quota: ByteQuota, **kwargs) -> dict:
    kwargs.setdefault("timeout_s", 0.15)
    return ProbeEngine(
        "127.0.0.1", receiver.port, quota=quota, **kwargs
    ).run()


def test_largest_group_fits_under_minimum_quota():
    # min_size=1400 forces a 1472-byte candidate; group = 1472 + 64 = 1536
    quota = make_quota(MIN_QUOTA_BYTES)
    with FakeReceiver() as rx:
        rep = run(rx, quota, min_size=1400, max_size=1472, timeout_s=0.1)
    assert rep["verdict"] == "ok"
    assert rep["max_confirmed_size"] == 1472
    assert rep["inflight_quota"]["limit_bytes"] == MIN_QUOTA_BYTES
    assert quota.snapshot()["inflight_bytes"] == 0


def test_quota_wait_is_excluded_from_receipt_timeout():
    # One engine holds the whole quota with a slow receiver; a second engine's
    # group then has to wait for the bytes. Its short receipt window must start
    # only after the wait, so its group still gets its receipts on time.
    quota = make_quota(MIN_QUOTA_BYTES)
    with FakeReceiver(delay_s=0.2) as slow, FakeReceiver() as fast:
        a = ProbeEngine(
            "127.0.0.1", slow.port, quota=quota,
            timeout_s=0.4, final_wait_s=0.3,
        )
        t = threading.Thread(target=a.run)
        t.start()
        time.sleep(0.1)  # let run A reserve 1536B and block on slow receipts

        b = ProbeEngine(
            "127.0.0.1", fast.port, quota=quota,
            timeout_s=0.1, final_wait_s=0.1,
        )
        rep_b = b.run()
        t.join()

    assert rep_b["groups"], "B sent at least one group"
    first_group = rep_b["groups"][0]
    assert first_group["quota_wait_ms"] > 0  # B really queued behind A
    assert first_group["candidate_result"] == "confirmed"
    assert first_group["control_on_time"] is True
    # The wait must not have been charged against the 100ms receipt window.
    assert rep_b["inflight_quota"]["waited_for_quota_ms"] >= first_group["quota_wait_ms"]


def test_timeout_releases_bytes_and_late_receipt_does_not_return_twice():
    # Slow receipts: the group times out (bytes returned), then a late valid
    # receipt still confirms the size but must not release the lease again.
    quota = make_quota(MIN_QUOTA_BYTES)
    with FakeReceiver(delay_s=0.3) as rx:
        rep = run(rx, quota, timeout_s=0.1, final_wait_s=0.5)

    assert rep["max_confirmed_size"] == 1472  # late receipt still evidence
    first = rep["groups"][0]
    assert first["candidate_result"] == "inconclusive"
    assert quota.snapshot()["inflight_bytes"] == 0
    by_reason = quota.released_bytes_by_reason
    # Every packet ended accounted exactly once: no double return on late ack.
    assert by_reason["group_timeout"] >= 64
    assert sum(by_reason.values()) == sum(p["size"] for p in rep["packets"])
    assert quota.release_calls == rep["packets_sent"]
    for pkt in rep["packets"]:
        assert pkt["quota_release_reason"] in ("group_timeout", "receipt")


def test_send_failure_returns_unsent_bytes_immediately(monkeypatch):
    quota = make_quota(4096)
    real_sendto = socket.socket.sendto
    calls = {"n": 0}

    def flaky_sendto(self, data, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # first packet (the candidate) cannot leave
            raise OSError("simulated local send failure")
        return real_sendto(self, data, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "sendto", flaky_sendto)
    with FakeReceiver() as rx:
        rep = run(rx, quota, timeout_s=0.1, final_wait_s=0.1)

    packets = rep["packets"]
    assert packets[0]["status"] == "send_failed"
    assert packets[0]["quota_release_reason"] == "send_failed"
    assert packets[1]["status"] == "confirmed"
    assert packets[1]["quota_release_reason"] == "receipt"
    assert quota.snapshot()["inflight_bytes"] == 0
    # failed send does not consume budget as an in-flight reservation
    assert quota.released_bytes_by_reason["send_failed"] == packets[0]["size"]


def test_parallel_runs_share_one_process_quota():
    # Two engines, one process-wide quota of 1536B: while run A's group is
    # awaiting receipts, run B can only send the moment A frees bytes.
    quota = make_quota(MIN_QUOTA_BYTES)
    results = {}
    with FakeReceiver(drop_threshold=1400) as rxA, FakeReceiver() as rxB:
        def run_a():
            results["a"] = ProbeEngine(
                "127.0.0.1", rxA.port, quota=quota, timeout_s=0.15
            ).run()

        t = threading.Thread(target=run_a)
        t.start()
        time.sleep(0.05)  # A reserves first, B must queue behind it
        results["b"] = ProbeEngine(
            "127.0.0.1", rxB.port, quota=quota, timeout_s=0.15
        ).run()
        t.join()

    rep_a, rep_b = results["a"], results["b"]
    assert rep_b["groups"][0]["quota_wait_ms"] > 0
    assert rep_a["verdict"] == "ok" and rep_b["verdict"] == "ok"
    assert quota.snapshot()["inflight_bytes"] == 0
    # Every reserved byte across both runs was released exactly once.
    total_bytes = sum(p["size"] for p in rep_a["packets"]) + sum(
        p["size"] for p in rep_b["packets"]
    )
    assert sum(quota.released_bytes_by_reason.values()) == total_bytes
    assert quota.release_calls == rep_a["packets_sent"] + rep_b["packets_sent"]
