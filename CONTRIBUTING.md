# Contributing to NimbusGuard

Thanks for your interest in contributing! This guide gets you from clone to first pull request in about 30 minutes.

## Setup

```bash
make install
```

Terminal 1 (API + simulator on :8000):

```bash
make backend
```

Terminal 2 (dashboard on :5173):

```bash
make frontend
```

Sign in as `admin` / `nimbusguard`, open **Simulator**, and inject a fault to see the full loop.

## Where things live

| I want to... | Look at |
|---|---|
| add a remediation | `backend/app/playbooks/*.yaml` ([guide](docs/playbooks.md)) |
| add a metric source | `backend/app/collectors/` (emit `Sample` / `LogEvent` / `Flow`) |
| improve detection | `backend/app/intelligence/anomaly.py`, then run `make evaluate` before and after |
| change RCA logic | `backend/app/intelligence/rca.py` and `tests/test_intelligence.py` |
| add an API endpoint | `backend/app/api/` |
| change the UI | `frontend/src/pages/`, with shared bits in `frontend/src/components/` |

Read [docs/architecture.md](docs/architecture.md) for how the layers fit together.

## Ground rules

* `make lint test` must pass. CI also runs the detection-accuracy gate (≥ 80% recall and precision).
* If you change anything in `intelligence/`, paste the `make evaluate` output before and after into your PR description.
* New automation must be safe by default. Add a `verify` step and a `rollback` block, and mark changes that can affect other devices `impact: downstream`.
* Keep pull requests focused. One feature or fix per PR.

## Suggested first issues

* An Azure Monitor collector implementing `CloudCollector`
* An SNMPv3 option in `collectors/snmp.py`
* Exporting incidents as CSV from the Incidents page
* Dark/light theme toggle
