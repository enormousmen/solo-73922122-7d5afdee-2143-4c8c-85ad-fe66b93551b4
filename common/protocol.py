"""Wire format shared by the prober (API) and the fixed receiver.

All integers are big-endian. One header layout is used for both directions:

    magic     4s   b"PMTU"
    type      B    0 = probe, 1 = receipt
    flags     B    bit0: control packet (64-byte reference)
    reserved  H
    run_id    8s   identifies one probe run
    group     I    group number (candidate + control share a group)
    seq       I    probe number, unique per packet within a run
    value     I    probe: total payload size / receipt: received length

Probe packets are zero-padded up to `value` bytes. Receipts are short
(header only, 28 bytes) and carry the probe number plus the length the
receiver actually got, so the prober can verify both.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"PMTU"
TYPE_PROBE = 0
TYPE_RECEIPT = 1
FLAG_CONTROL = 0x01

HEADER = struct.Struct("!4sBBH8sIII")
HEADER_SIZE = HEADER.size  # 28

MIN_PACKET_SIZE = 64
MAX_PACKET_SIZE = 1472
CONTROL_SIZE = 64


@dataclass(frozen=True)
class Packet:
    ptype: int
    flags: int
    run_id: bytes
    group: int
    seq: int
    value: int      # probe: declared size / receipt: received length
    wire_len: int   # UDP payload length actually observed

    @property
    def is_control(self) -> bool:
        return bool(self.flags & FLAG_CONTROL)


def build_probe(run_id: bytes, group: int, seq: int, size: int, control: bool) -> bytes:
    if len(run_id) != 8:
        raise ValueError("run_id must be 8 bytes")
    if not HEADER_SIZE <= size <= MAX_PACKET_SIZE:
        raise ValueError(f"size must be within {HEADER_SIZE}..{MAX_PACKET_SIZE}")
    flags = FLAG_CONTROL if control else 0
    header = HEADER.pack(MAGIC, TYPE_PROBE, flags, 0, run_id, group, seq, size)
    return header + b"\0" * (size - HEADER_SIZE)


def build_receipt(run_id: bytes, group: int, seq: int, recv_len: int) -> bytes:
    if len(run_id) != 8:
        raise ValueError("run_id must be 8 bytes")
    return HEADER.pack(MAGIC, TYPE_RECEIPT, 0, 0, run_id, group, seq, recv_len)


def parse_packet(data: bytes) -> Packet | None:
    if len(data) < HEADER_SIZE:
        return None
    magic, ptype, flags, _reserved, run_id, group, seq, value = HEADER.unpack(
        data[:HEADER_SIZE]
    )
    if magic != MAGIC or ptype not in (TYPE_PROBE, TYPE_RECEIPT):
        return None
    return Packet(
        ptype=ptype,
        flags=flags,
        run_id=run_id,
        group=group,
        seq=seq,
        value=value,
        wire_len=len(data),
    )
