"""Engine rule tests, run against a local programmable receiver."""
from __future__ import annotations

from common.protocol import (
    TYPE_PROBE,
    TYPE_RECEIPT,
    build_probe,
    build_receipt,
    parse_packet,
)
from fake_receiver import FakeReceiver
from prober.app.engine import MAX_BUDGET, ProbeEngine


def run_engine(receiver: FakeReceiver, **kwargs) -> dict:
    kwargs.setdefault("timeout_s", 0.15)
    return ProbeEngine("127.0.0.1", receiver.port, **kwargs).run()


def test_protocol_roundtrip():
    blob = build_probe(b"12345678", 3, 41, 200, control=False)
    assert len(blob) == 200
    pkt = parse_packet(blob)
    assert pkt.ptype == TYPE_PROBE
    assert (pkt.group, pkt.seq, pkt.value, pkt.wire_len) == (3, 41, 200, 200)
    assert not pkt.is_control

    ctrl = parse_packet(build_probe(b"12345678", 3, 42, 64, control=True))
    assert ctrl.is_control and ctrl.wire_len == 64

    ack = parse_packet(build_receipt(b"12345678", 3, 41, 200))
    assert ack.ptype == TYPE_RECEIPT and ack.seq == 41 and ack.value == 200

    assert parse_packet(b"garbage") is None
    assert parse_packet(b"") is None


def test_no_loss_confirms_upper_limit():
    with FakeReceiver() as rx:
        rep = run_engine(rx)
    assert rep["verdict"] == "ok"
    assert rep["stop_reason"] == "upper_limit_confirmed"
    assert rep["max_confirmed_size"] == 1472
    assert rep["min_suspected_oversize"] is None
    assert rep["packets_sent"] == 2  # one candidate + one control
    assert rep["df_set"] is True


def test_threshold_drop_converges_within_8_bytes():
    with FakeReceiver(drop_threshold=1400) as rx:
        rep = run_engine(rx)
    mc, ms = rep["max_confirmed_size"], rep["min_suspected_oversize"]
    assert rep["verdict"] == "ok"
    assert mc <= 1400 < ms
    assert ms - mc <= 8
    assert rep["packets_sent"] <= MAX_BUDGET


def test_threshold_just_above_minimum_stays_within_budget():
    # worst case for the binary search: almost everything is dropped
    with FakeReceiver(drop_threshold=64) as rx:
        rep = run_engine(rx)
    assert rep["verdict"] == "ok"
    assert rep["max_confirmed_size"] == 64
    assert rep["min_suspected_oversize"] <= 72
    assert rep["packets_sent"] <= MAX_BUDGET


def test_suspect_is_always_above_confirmed():
    with FakeReceiver(drop_threshold=1000) as rx:
        rep = run_engine(rx)
    assert rep["min_suspected_oversize"] > rep["max_confirmed_size"]


def test_two_consecutive_timeouts_required_for_suspect():
    # drop only the FIRST group: the candidate must not be flagged
    with FakeReceiver(drop_groups={1}) as rx:
        rep = run_engine(rx)
    assert rep["verdict"] == "ok"
    assert rep["max_confirmed_size"] == 1472
    assert rep["min_suspected_oversize"] is None
    g1 = rep["groups"][0]
    assert g1["candidate_result"] == "inconclusive"
    assert g1["control_on_time"] is False


def test_whole_group_loss_does_not_block_convergence():
    with FakeReceiver(drop_threshold=1400, drop_groups={5}) as rx:
        rep = run_engine(rx)
    assert rep["verdict"] == "ok"
    g5 = rep["groups"][4]
    assert g5["candidate_result"] == "inconclusive"
    assert g5["strikes_after"] == 0
    assert rep["min_suspected_oversize"] - rep["max_confirmed_size"] <= 8


def test_duplicate_receipts_counted_once():
    with FakeReceiver(duplicate=True) as rx:
        rep = run_engine(rx)
    assert rep["max_confirmed_size"] == 1472
    assert rep["receipts"]["duplicates"] >= 2
    confirmed = [p for p in rep["packets"] if p["status"] == "confirmed"]
    assert len(confirmed) == rep["receipts"]["valid"] == 2


def test_late_receipts_still_confirm():
    # receipts arrive after their group's window but before the run ends
    with FakeReceiver(delay_s=0.25) as rx:
        rep = run_engine(rx, timeout_s=0.1, final_wait_s=0.4)
    assert rep["verdict"] == "ok"
    assert rep["max_confirmed_size"] == 1472
    assert rep["groups"][0]["candidate_result"] == "inconclusive"


def test_blackhole_prompts_retest():
    with FakeReceiver(drop_all=True) as rx:
        rep = run_engine(rx, timeout_s=0.05)
    assert rep["verdict"] == "retest"
    assert rep["max_confirmed_size"] is None
    assert rep["min_suspected_oversize"] is None
    assert "重测" in rep["message"]


def test_budget_is_capped():
    rep = None
    with FakeReceiver(drop_threshold=64) as rx:
        rep = run_engine(rx, budget=MAX_BUDGET)
    assert rep["packets_sent"] <= MAX_BUDGET
