# Deployment guide

## 1. Choose a mode

| | Single container | Docker Compose stack |
|---|---|---|
| Command | `docker run -p 8000:8000 nimbusguard` | `docker compose up -d` |
| Database | SQLite in `/data` (mount a volume) | TimescaleDB |
| Cache / bus | in-process | Redis |
| Processes | API + worker in one | API, worker, Caddy (TLS) |
| Good for | demos, evaluation, very small sites | production, more than ~50 devices |

## 2. Configure

Copy `.env.example` to `.env`. Every setting is an `NG_*` environment variable, defined in `backend/app/config.py`. At minimum, change:

```bash
NG_SECRET_KEY=$(openssl rand -hex 32)
NG_ADMIN_PASSWORD=<strong password>
NG_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
POSTGRES_PASSWORD=<strong password>
```

Remove the demo time acceleration (`NG_TIME_SCALE`, `NG_PREDICTION_LOOKBACK_MINUTES`, `NG_PERSIST_INTERVAL_SECONDS`, `NG_ANALYSIS_INTERVAL_SECONDS`) so the defaults apply: real time, a 6-hour forecast lookback, and 30-second persistence.

## 3. Connect your network

1. Disable the simulator: `NG_SIMULATOR_ENABLED=false`.
2. Enable collectors: `NG_SNMP_ENABLED=true`, `NG_SYSLOG_ENABLED=true`, `NG_NETFLOW_ENABLED=true`.
3. Register devices (operator role):

   ```bash
   curl -X POST https://nimbus.example.org/api/devices \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"core-sw-1","type":"switch","ip":"10.0.0.2","criticality":"high",
          "collector":"snmp","snmp_community":"<community>","snmp_profile":"cisco-ios",
          "capacity_mbps":10000,"upstream_id":"<router id>"}'
   ```

   Setting `upstream_id` builds the dependency graph, which root-cause analysis and impact analysis rely on.
4. Point device syslog at `<worker host>:5514/udp`. On Cisco IOS:

   ```
   logging host <ip> transport udp port 5514
   ```

5. Export NetFlow v5 to `<worker host>:2055/udp`.
6. Allow about a week of learning before trusting anomaly alerts. Threshold rules work from day one.

## 4. Remediation on real devices

Real devices use `DryRunDriver`: each step records the exact commands it would run, and changes nothing. Review these on the Remediation page. To execute changes for real, implement a driver with the same `execute()` interface (for example with Netmiko over SSH), and return it from `Runtime._driver_for()`. Keep `NG_AUTO_REMEDIATION_ENABLED=false` at first, so every fix requires approval.

## 5. Notifications

* **Phone push**: install the ntfy app, subscribe to a hard-to-guess topic, and set `NG_NTFY_TOPIC`. Self-host ntfy with `NG_NTFY_SERVER`.
* **Chat**: set `NG_WEBHOOK_URL` to a Slack/Teams/Discord incoming webhook.

## 6. TLS

`deploy/Caddyfile` serves `localhost` with Caddy's internal CA. For a public hostname, replace `localhost` with the domain, publish ports 80 and 443, and Caddy will obtain a Let's Encrypt certificate automatically.

## 7. Operations

* Health check: `GET /healthz`; system info: `GET /api/system/info`
* Data retention: 90 days for raw telemetry (Timescale retention policy), with compression after 7 days
* Backups: `pg_dump` the `nimbusguard` database. The audit chain can be re-verified after restore with `GET /api/audit/verify`.
* Scaling: the API is stateless and can be replicated behind Caddy. Run exactly **one** worker.
