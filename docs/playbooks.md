# Writing remediation playbooks

Playbooks live in `backend/app/playbooks/*.yaml` and are loaded at startup. They are validated by `automation/playbooks.py`, and `tests/test_automation.py` checks that every playbook only uses actions a driver can execute.

## Schema

```yaml
name: interface-errors          # required, lowercase kebab-case, unique
title: Failing cable / port     # shown in the UI
description: Move traffic from a port with CRC errors onto the backup port.
triggers: [interface_errors]    # required: alert signatures that start this playbook
kinds: []                       # optional: restrict to alert kinds (anomaly|threshold|prediction|security|availability)
risk: medium                    # low | medium | high (high waits for the maintenance window)
impact: downstream              # downstream (default) | none - does the change affect devices behind this one?
requires_approval: false        # true = always wait for a human
steps:                          # required, executed in order
  - action: snapshot_config
  - action: switch_to_backup_port
    params: {from_port: "Gi0/24", to_port: "Gi0/23"}
  - action: wait
    params: {seconds: 60}       # divided by NG_TIME_SCALE in demo mode
  - action: verify
    params: {metric: if_errors, below: 10}
rollback:                       # optional, runs if any step fails
  - action: restore_config
```

### Step fields

| Field | Meaning |
|---|---|
| `action` | built-in (`wait`, `verify`, `notify`, `escalate`) or a driver action |
| `params` | templated with `{placeholders}` from the run context |
| `when: key` | run only if `context[key]` is truthy |
| `unless: key` | skip if `context[key]` is truthy |

### Run context

The context starts with `device`, `device_id`, `ip`, `primary_service` and any alert details (`source_ips`, `source_ip`, `destination_ip`). Each step's result is merged into it, so later steps can use earlier outputs: `identify_top_process` returns `top_process`, which `restart_process` uses. A placeholder that is the *entire* value (`"{source_ips}"`) keeps its type, so lists stay lists.

### Built-in actions

| Action | Behaviour |
|---|---|
| `wait` | sleep `seconds / NG_TIME_SCALE` |
| `verify` | check the device's latest values, `{metric, below/above}` or `{reachable: true}`, with 3 attempts. **Failure triggers rollback.** |
| `notify` | push, webhook and in-app notification |
| `escalate` | notify with high priority and **stop** the run (status `escalated`, no rollback) |

### Driver actions

`snapshot_config`, `restore_config`, `restart_process`, `restart_service`, `switch_to_backup_port`, `reroute_traffic`, `archive_logs`, `block_ip`, `unblock_ip`, `quarantine_host`, `release_host`, `power_cycle`, `identify_top_process`, `check_process_critical`, `ping`.

For real devices, `DryRunDriver` maps each one to a vendor command template in `automation/drivers.py`, and records the commands it *would* run. Add a template there when you add an action.

## Signatures

| Signature | Raised by |
|---|---|
| `cpu_high`, `memory_high`, `disk_full`, `overheating` | anomaly z-score or threshold rule on that metric |
| `interface_errors` | anomaly on `if_errors` |
| `degraded_path` | latency / packet-loss anomaly or threshold (usually a *symptom*, so RCA looks upstream) |
| `link_saturation` | utilisation > 90% rule, or a saturation forecast |
| `memory_exhaustion`, `disk_exhaustion`, `failure_risk` | forecaster |
| `brute_force`, `exfiltration` | Syslog / NetFlow security rules |
| `device_down` | availability check (only the most upstream unreachable device is remediated) |
| `multivariate`, `traffic_spike` | Isolation Forest / traffic anomalies (no default playbook) |

## Testing a playbook

1. Add the YAML file and run `make test`, which validates every action name.
2. Start the demo (`make backend`), open **Simulator**, and inject the matching fault.
3. Watch the run on the **Remediation** page. Tick **Stubborn fault** to exercise your rollback path.
