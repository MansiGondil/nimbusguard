"""Application settings, loaded from environment variables (prefix ``NG_``)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NG_", env_file=".env", extra="ignore")

    app_name: str = "NimbusGuard"
    environment: str = "development"

    # Storage. SQLite + in-memory cache gives a zero-dependency demo mode;
    # point these at TimescaleDB / Redis for a real deployment.
    database_url: str = "sqlite+aiosqlite:///./nimbusguard.db"
    redis_url: str | None = None

    # Security
    secret_key: str = "change-me-in-production-please-32b"
    # Fernet key used to encrypt device credentials at rest. Derived from
    # secret_key when not set explicitly.
    encryption_key: str | None = None
    access_token_minutes: int = 60 * 8
    admin_username: str = "admin"
    admin_password: str = "nimbusguard"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # Workers. When false the API process only serves requests; run
    # ``python -m app.worker`` separately (see docker-compose.yml).
    run_workers: bool = True
    simulator_enabled: bool = True
    simulator_devices: int = 24
    simulator_interval_seconds: float = 5.0
    simulator_backfill_days: int = 7
    # Random fault injection every N minutes (0 disables) keeps a demo lively.
    simulator_chaos_minutes: float = 0
    # >1 speeds up demo time: faults evolve faster, and rule durations and
    # playbook waits shrink accordingly ("CPU > 90% for 5 min" -> 30 s at 10x).
    time_scale: float = 1.0
    snmp_enabled: bool = False
    snmp_interval_seconds: int = 60
    cloud_enabled: bool = False  # AWS CloudWatch (pip install boto3)
    cloud_interval_seconds: int = 300
    syslog_enabled: bool = False
    syslog_port: int = 5514
    netflow_enabled: bool = False
    netflow_port: int = 2055

    # Intelligence
    baseline_min_samples: int = 12
    anomaly_z_threshold: float = 3.5
    anomaly_consecutive: int = 2
    prediction_horizon_hours: int = 24
    prediction_lookback_minutes: int = 360
    analysis_interval_seconds: float = 30.0
    persist_interval_seconds: float = 30.0

    # Automation safety gates
    max_fixes_per_device_per_hour: int = 3
    impact_approval_threshold: int = 3
    maintenance_window: str = "00:00-06:00"  # UTC, high-risk actions only
    auto_remediation_enabled: bool = True
    remediation_cooldown_minutes: float = 15.0

    # Executive dashboard cost model (conservative NGO/school defaults)
    downtime_cost_per_hour: float = 500.0
    avg_incident_hours_avoided: float = 1.5

    # Notifications (mobile push via ntfy.sh, generic webhook)
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    webhook_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
