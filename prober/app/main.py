"""FastAPI front-end for the UDP PMTU prober.

POST /probe starts one probe run against the fixed receiver configured via
RECEIVER_HOST / RECEIVER_PORT. The run is synchronous (UDP probing with
timeouts); FastAPI executes it in the threadpool. Runs are serialized with a
lock so interleaved runs cannot confuse the operator-facing drill scenarios.
"""
from __future__ import annotations

import os
import threading

from fastapi import FastAPI
from pydantic import BaseModel, Field, model_validator

from common.protocol import MAX_PACKET_SIZE, MIN_PACKET_SIZE
from prober.app.engine import MAX_BUDGET, ProbeEngine

RECEIVER_HOST = os.environ.get("RECEIVER_HOST", "127.0.0.1")
RECEIVER_PORT = int(os.environ.get("RECEIVER_PORT", "5055"))

app = FastAPI(title="udp-pmtu-prober", version="1.0.0")
_run_lock = threading.Lock()


class ProbeRequest(BaseModel):
    min_size: int = Field(
        MIN_PACKET_SIZE, ge=MIN_PACKET_SIZE, le=MAX_PACKET_SIZE,
        description="搜索下界（字节）",
    )
    max_size: int = Field(
        MAX_PACKET_SIZE, ge=MIN_PACKET_SIZE, le=MAX_PACKET_SIZE,
        description="探测上限（字节）",
    )
    timeout_ms: int = Field(
        500, ge=50, le=10_000, description="每组等待回执的时间（毫秒）",
    )
    final_wait_ms: int | None = Field(
        None, ge=0, le=30_000,
        description="结束前等待迟到回执的时间（毫秒），默认同 timeout_ms",
    )
    budget: int = Field(
        MAX_BUDGET, ge=4, le=MAX_BUDGET,
        description="本次探测最多发送的报文数（含对照），硬上限 48",
    )

    @model_validator(mode="after")
    def _check_range(self):
        if self.max_size <= self.min_size:
            raise ValueError("max_size 必须大于 min_size")
        return self


@app.get("/healthz")
def healthz():
    return {"status": "ok", "receiver": f"{RECEIVER_HOST}:{RECEIVER_PORT}"}


@app.post("/probe")
def probe(req: ProbeRequest):
    engine = ProbeEngine(
        host=RECEIVER_HOST,
        port=RECEIVER_PORT,
        min_size=req.min_size,
        max_size=req.max_size,
        timeout_s=req.timeout_ms / 1000.0,
        budget=req.budget,
        final_wait_s=(
            req.final_wait_ms if req.final_wait_ms is not None else req.timeout_ms
        ) / 1000.0,
    )
    with _run_lock:
        return engine.run()
