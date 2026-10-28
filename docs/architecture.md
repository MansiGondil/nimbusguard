# Architecture

NimbusGuard follows the five-layer design from the system overview: **sensors, memory, brain, healer, dashboard**. Each layer is a separate Python package with a narrow interface, so it can be tested in isolation and later split into its own service.

```
backend/app/
├── collectors/     Layer 1  Data collection   (SNMP, Syslog, NetFlow, CloudWatch, simulator)
├── models.py, db.py, cache.py
│                   Layer 2  Storage           (TimescaleDB / PostgreSQL / Redis)
├── intelligence/   Layer 3  Intelligence      (baselines, anomalies, forecasts, RCA, health)
├── automation/     Layer 4  Automation        (playbooks, safety gates, executor, drivers)
└── api/ + frontend Layer 5  Presentation      (REST, WebSocket, React dashboard)
```

`services/pipeline.py` wires the layers into a streaming loop, and `services/runtime.py` owns the lifecycle (collectors, background loops, command handling).

## Process model

| Mode | Processes | Storage | When |
|---|---|---|---|
| Demo (`NG_RUN_WORKERS=true`, default) | one: API + worker in-process | SQLite + in-memory cache/bus | laptop, single container, tests |
| Split (`NG_RUN_WORKERS=false` + `python -m app.worker`) | API (stateless, can scale out) + one worker | TimescaleDB + Redis | `docker compose` / production |

In split mode the API never touches the simulator or the executor directly. It **dispatches commands** (`inject`, `request_run`, `execute_run`, `reload_devices`) on the Redis channel `nimbusguard:commands`. The worker **publishes events** (`metrics`, `alert`, `incident`, `prediction`, `remediation`, `health`, `notification`, `logs`) on `nimbusguard:events`, which every API instance relays to its WebSocket clients. `services/runtime.dispatch()` hides the difference: it calls the local runtime when one exists, and publishes otherwise.

Startup is serialised with a PostgreSQL advisory lock (`db.startup_lock`), so concurrent API and worker boots don't race on schema creation or seeding. The audit log takes a second advisory lock, so that writers in different processes extend one hash chain.

## Layer 1: Collectors

Every collector emits the same three dataclasses (`collectors/base.py`):

* `Sample(device_id, time, values: dict[str, float])`: metrics such as `cpu`, `memory`, `disk`, `traffic_in/out` (Mbps), `if_errors` (per min), `latency` (ms), `packet_loss` (%) and `temperature`
* `LogEvent`: a parsed syslog message with a category (`auth`, `config`, `interface`, `security`, `system`) and the source IP
* `Flow`: a NetFlow record

| Collector | Implementation |
|---|---|
| SNMP | `pysnmp` v7 asyncio `get_cmd`, with vendor OID profiles (`net-snmp`, `cisco-ios`). Counters (`ifHCInOctets`, `ifInErrors`) are converted to rates between polls, with wrap detection. Poll latency doubles as a reachability/latency metric. |
| Syslog | asyncio UDP server; RFC 5424 and RFC 3164 parsers; regex categorisation |
| NetFlow | asyncio UDP server; NetFlow v5 decoder (and encoder, for tests and replay) |
| Cloud | `CloudWatchCollector` (EC2 CPU and network) via `boto3`; Azure and VMware are roadmap items behind the same interface |
| Simulator | `NetworkSimulator`: device profiles with business-hours seasonality and weekday/weekend patterns, plus 8 injectable faults that propagate through the topology. It is also the remediation *driver* in demo mode, so fixes have a measurable effect. |

## Layer 2: Storage

| Store | Contents |
|---|---|
| `metrics` (Timescale hypertable) | `(time, device_id, metric, value)`, compressed after 7 days (segment by device and metric), retained 90 days. Index on `(device_id, metric, time)`. |
| `syslog_events`, `flow_records` (hypertables) | raw events, 90-day retention |
| relational tables | `devices`, `device_links` (topology), `users`, `baselines`, `alerts`, `incidents`, `predictions`, `playbook_runs`, `audit_log` |
| Redis | `latest:{device}` and `latest:all` (fresh state for the API), `health:score`, `sim:faults`, pub/sub channels |

Live samples arrive every few seconds. The pipeline **downsamples** them to `NG_PERSIST_INTERVAL_SECONDS` means before writing, so storage grows with time, not with the poll rate. Real-time charts are fed straight from the WebSocket.

## Layer 3: Intelligence

### Baselines (`intelligence/baseline.py`)

Running mean and variance (Welford) are kept per `(device, metric, bucket)` at three granularities: hour-of-week (0-167), hour-of-day, and global. `expect()` returns the most specific bucket with at least `NG_BASELINE_MIN_SAMPLES` samples, so a new device is useful within hours and becomes precise after a week. Per-metric standard-deviation floors stop near-constant metrics (0 errors/min) from producing huge z-scores on tiny changes.

Online learning is **rate-limited to the baseline's training resolution (5 min)**, and skips values with z > 2. Without both rules, a slowly emerging fault sampled every 5 s would swamp its hour bucket and be "learned" as normal. This was a real bug, found by the accuracy evaluation.

### Anomaly ensemble (`intelligence/anomaly.py`)

1. **Seasonal z-score** against the expectation. It raises after `NG_ANOMALY_CONSECUTIVE` abnormal samples, and clears after 3 normal ones.
2. **Duration-held safety rules**, e.g. CPU > 90% for 5 minutes, memory > 92% for 2 minutes, packet loss > 5% for 1 minute, link utilisation > 90% for 2 minutes. They work before any baseline exists.
3. **Isolation Forest** trained on per-sample z-score vectors. It flags unusual *combinations* when no univariate detector fired, and attaches an `ai_confidence` to every anomaly.

Each detection carries a **signature** (`cpu_high`, `interface_errors`, `degraded_path`, ...), which RCA and playbook routing use.

### Forecasting (`intelligence/prediction.py`)

* `time_to_threshold`: least-squares trend over the full lookback window, then the recent half and quarter, so "flat, then leaking" is caught early. To count, a trend needs r² ≥ 0.6, a rise that clearly exceeds the residual noise, and latest readings that still sit on the line (a restart breaks the trend). The output is an ETA and a confidence.
* `failure_risk`: a logistic model over symptom features (CPU-spike ratio, memory slope, interface-error trend, packet loss, temperature), producing "85% chance of failure within 24h".

Forecasts become `prediction` alerts. These can trigger *preventive* playbooks: the memory-leak restart happens before the threshold is crossed.

### Root-cause analysis (`intelligence/rca.py`)

Open alerts are clustered when their devices are related in the dependency graph (one is an ancestor of the other) and they started within 10 minutes of each other. Security alerts only group on the same device. In each cluster:

* the **root** is the alerting device with no alerting ancestor (ties are broken by the most causal signature, then the earliest alert)
* the **diagnosis and recommendation** come from the root's signature
* the **investigation chain** is the upward path from the furthest impacted device to the root, with each hop marked normal or abnormal
* the **impact** is the number of downstream devices, alerting devices and grouped alerts

Clusters map onto persistent `incidents`, and an incident resolves when all its alerts resolve.

### Health score (`intelligence/health.py`)

Device health starts at 100 and deducts for open-alert severity, headroom (CPU/memory/disk above 70%) and packet loss. Down devices score 0. Network health is the criticality-weighted mean (critical = 2x, low = 0.5x).

## Layer 4: Automation

```
alert raised ──► playbook_for(signature, kind)
                    │
                    ▼
             safety.evaluate()  ── block ──► run recorded as "blocked"
                    │ approval
                    ├──────────► "pending_approval" ─► push notification ─► operator approves
                    ▼ approved
             executor: steps (when/unless, templated params)
                    │
              verify ok? ── no ──► rollback steps ─► "rolled_back" + escalate
                    │ yes
                    ▼
               "succeeded" ─► incident.prevented = true
```

**Safety gates** (`automation/safety.py`), evaluated and stored on every run:

| Gate | Effect |
|---|---|
| `automation_enabled` | global kill switch: everything needs approval |
| `maintenance_mode` | the device is being worked on by a human: block |
| `change_frequency` | at most `NG_MAX_FIXES_PER_DEVICE_PER_HOUR` (3) executed fixes per device per hour: block |
| `cooldown` | the same fix just succeeded on this device: block while observing |
| `change_window` | high-risk playbooks outside `NG_MAINTENANCE_WINDOW`: approval |
| `impact_analysis` | ≥ `NG_IMPACT_APPROVAL_THRESHOLD` downstream devices, for playbooks with `impact: downstream`: approval |
| `critical_system` | non-low-risk change on a `critical` device: approval |
| `playbook_policy` | `requires_approval: true`: approval |

**Drivers** (`automation/drivers.py`): `SimulatorDriver` applies actions to the simulation. `DryRunDriver` renders vendor commands for real devices, without executing them. A live driver implements `async execute(action, device, params) -> dict`.

## Layer 5: Presentation

* **REST API** (FastAPI, OpenAPI at `/docs`), with JWT bearer auth and role guards (`viewer` < `operator` < `admin`)
* **WebSocket** `/ws?token=` streams every event. The React app keeps a short live history per device for real-time charts, and refetches views when relevant events arrive.
* **Dashboard**: Executive, Operations, Devices, Device detail, Incidents (with RCA), Remediation (approvals, run timelines, playbook catalogue), Topology, Simulator, Audit
* **Notifications**: ntfy push (mobile), webhook (Slack/Teams/Discord-compatible), in-app toasts

## Security

* Passwords hashed with bcrypt; JWTs signed with `NG_SECRET_KEY`
* Device credentials encrypted with Fernet (`NG_ENCRYPTION_KEY`, or a key derived from the secret), never returned by the API
* TLS termination via Caddy with HSTS and security headers. Use a real domain for Let's Encrypt.
* Audit log: each entry stores `sha256(prev_hash + entry)`. `GET /api/audit/verify` recomputes the chain and reports the first tampered row.
* The container runs as a non-root user
