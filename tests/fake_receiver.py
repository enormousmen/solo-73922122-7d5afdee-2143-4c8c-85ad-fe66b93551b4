"""Programmable in-process test endpoint used by the test suite."""
from __future__ import annotations

import socket
import threading

from common.protocol import TYPE_PROBE, build_receipt, parse_packet


class FakeReceiver:
    """UDP test endpoint on 127.0.0.1 with drill knobs mirroring receiver.py."""

    def __init__(
        self,
        drop_threshold: int = 0,
        drop_groups: frozenset | set = (),
        duplicate: bool = False,
        delay_s: float = 0.0,
        drop_all: bool = False,
    ):
        self.drop_threshold = drop_threshold
        self.drop_groups = set(drop_groups)
        self.duplicate = duplicate
        self.delay_s = delay_s
        self.drop_all = drop_all
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.sock.close()

    def _send_receipt(self, packet: bytes, addr) -> None:
        copies = 2 if self.duplicate else 1
        for _ in range(copies):
            try:
                self.sock.sendto(packet, addr)
            except OSError:
                return

    def _serve(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            pkt = parse_packet(data)
            if pkt is None or pkt.ptype != TYPE_PROBE:
                continue
            if self.drop_all or pkt.group in self.drop_groups:
                continue
            if self.drop_threshold and len(data) > self.drop_threshold:
                continue
            receipt = build_receipt(pkt.run_id, pkt.group, pkt.seq, len(data))
            if self.delay_s > 0:
                threading.Timer(
                    self.delay_s, self._send_receipt, args=(receipt, addr)
                ).start()
            else:
                self._send_receipt(receipt, addr)
