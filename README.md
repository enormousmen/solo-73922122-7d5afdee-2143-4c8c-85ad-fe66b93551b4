# udp-pmtu-prober

隧道加封装后，值班员用来核实可发多大 UDP 负载的探测工具：

- **API（FastAPI）**：`POST /probe` 接收探测请求（搜索区间 64～1472 字节），
  向**固定接收端**（`RECEIVER_HOST:RECEIVER_PORT`）发送**禁止分片**的 IPv4 UDP
  报文（`IP_MTU_DISCOVER = IP_PMTUDISC_DO`，即 DF=1）。
- **接收端**：对每个有效探针回一个 28 字节短回执，带回**探针号**和**实收长度**；
  同时是演练用的测试端，可配置门槛丢包、整组丢包、重复/延迟回执。

## 探测规则（与值班规范逐条对应）

| 规范 | 实现 |
| --- | --- |
| 每组用 64 字节报文作对照 | 每组 = 1 个候选包 + 1 个 64 字节对照包，同组号 |
| 同一候选连续两组超时且两组对照均按时回包，才记疑似超长 | 候选超时且本组对照按时回 → 记 1 次 strike，下一组重发同一候选；连续 2 次才记入 `suspects`；对照也丢的组记 `inconclusive`，不计数且清零连击 |
| 按探针号和实收长度核对回执 | 回执的 `run_id` 必须属于本次探测、`seq` 必须已发出、`recv_len` 必须等于该 `seq` 的发送长度，否则记 invalid 丢弃 |
| 探测结束前迟到的有效回执仍确认原长度 | 每个组的等待窗口和结束前的 final_wait 都会收取回执；按"长度"确认（同长度任一包的回执即确认该长度） |
| 重复回执只算一次 | 按 `seq` 去重，重复计入 `receipts.duplicates` |
| 疑似超长必须大于已确认可达长度 | 每次确认后剔除 `<= max_confirmed` 的疑似值；记录疑似时也校验 |
| 一次探测最多 48 个包（含对照） | `budget` 参数硬上限 48（API 校验 `le=48`），引擎超预算即停 |
| 返回最大已确认、最小疑似超长、探测记录 | 响应含 `max_confirmed_size` / `min_suspected_oversize` / `groups` / `packets` |
| 上限未确认且证据不足时提示重测 | `verdict=retest`，`message` 说明原因（路径不通 / 未定位疑似点 / 未收敛） |
| 门槛丢包且在区间内时，须确认上限或收窄到 8 字节以内 | 二分搜索，收敛条件 `min_suspected - max_confirmed <= 8`；上限可达时 1 组即确认 |

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
curl -s http://localhost:8000/healthz
```

### 示例 A：固定门槛丢包（>1400 字节的包被丢弃）

```bash
DROP_THRESHOLD=1400 docker compose up --build -d
curl -s -X POST http://localhost:8000/probe \
  -H 'Content-Type: application/json' -d '{}'
```

预期：`verdict=ok`，`max_confirmed_size ≤ 1400 < min_suspected_oversize`，
两者相差 ≤ 8 字节（实测 1400 / 1406，24 个包）。

### 示例 B：一整组丢包（第 5 组候选+对照全部丢失）

```bash
DROP_THRESHOLD=1400 DROP_GROUPS=5 docker compose up --build -d
curl -s -X POST http://localhost:8000/probe \
  -H 'Content-Type: application/json' -d '{}'
```

预期：第 5 组记为 `inconclusive`（对照也未回，**不计入疑似超长**），
下一组重发同一候选，最终仍收敛（实测 1400 / 1406，26 个包）。
接收端日志可见丢包原因：`docker compose logs receiver`。

### 其它演练旋钮（接收端环境变量）

| 变量 | 作用 |
| --- | --- |
| `DROP_THRESHOLD` | 丢弃负载大于该值的探针包（0=关闭） |
| `DROP_GROUPS` | 整组丢弃的组号，逗号分隔，如 `"3,7"` |
| `DUP_RECEIPTS` | `1` = 每个回执发两遍（重复回执演练） |
| `RECEIPT_DELAY_MS` | 回执延迟 N 毫秒（迟到回执演练） |

黑洞演练（提示重测）：`DROP_THRESHOLD=1 docker compose up --build -d`
→ 连对照都丢，`verdict=retest`，提示"路径可能不通，请重测"。

## API

`POST /probe`，请求体（均可选）：

```json
{
  "min_size": 64,        // 搜索下界，64..1472
  "max_size": 1472,      // 探测上限，64..1472 且 > min_size
  "timeout_ms": 500,     // 每组等待回执时间
  "final_wait_ms": null, // 结束前等迟到回执的时间，默认同 timeout_ms
  "budget": 48           // 本次最多发送报文数（含对照），硬上限 48
}
```

响应关键字段：`verdict`（`ok`/`retest`）、`message`、`max_confirmed_size`、
`min_suspected_oversize`、`boundary_gap`、`stop_reason`、`packets_sent`、
`receipts{valid,duplicates,invalid}`、`groups[]`（每组的候选结果/对照是否按时/
连击数）、`packets[]`（每包的探针号/长度/状态/RTT）。

## 协议格式（大端，28 字节定长头）

```
magic 4s="PMTU" | type B(0=probe,1=receipt) | flags B(bit0=control) | reserved H
run_id 8s | group I | seq I | value I
```

探针包 `value`=总负载长度并零填充到该长度；回执包 `value`=实收长度，仅头部 28 字节。

## 本地自测（无 Docker）

```bash
pip install -r prober/requirements.txt pytest httpx
PYTHONPATH=. python -m pytest tests/ -v          # 14 个规则/接口测试

# 手工端到端（两个终端）
PYTHONPATH=. PORT=5055 DROP_THRESHOLD=1400 python receiver/receiver.py
PYTHONPATH=. RECEIVER_HOST=127.0.0.1 RECEIVER_PORT=5055 \
  uvicorn prober.app.main:app --port 8000
```

## 目录结构

```
common/protocol.py     # 共享线协议
prober/app/engine.py   # 探测引擎（规则核心）
prober/app/main.py     # FastAPI 入口
receiver/receiver.py   # 固定接收端 / 测试端
tests/                 # pytest：假接收端 + 规则测试 + API 测试
docker-compose.yml     # api + receiver 两服务
```
