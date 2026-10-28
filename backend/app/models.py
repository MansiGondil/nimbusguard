"""SQLAlchemy ORM models.

Time-series tables (``metrics``, ``syslog_events``, ``flow_records``) use a
primary key that includes ``time`` so they can be converted into
TimescaleDB hypertables.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC DateTime that also round-trips tz info on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        if value is not None and dialect.name == "sqlite":
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer|operator|admin
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(32))  # router|switch|firewall|server|access_point|cloud
    ip: Mapped[str | None] = mapped_column(String(64))
    site: Mapped[str] = mapped_column(String(64), default="main")
    vendor: Mapped[str | None] = mapped_column(String(64))
    criticality: Mapped[str] = mapped_column(String(16), default="medium")  # low|medium|high|critical
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # up|degraded|down|unknown
    collector: Mapped[str] = mapped_column(String(16), default="simulated")  # simulated|snmp|cloud
    credentials_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted JSON
    services: Mapped[list] = mapped_column(JSON, default=list)
    capacity_mbps: Mapped[float] = mapped_column(Float, default=1000.0)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(TZDateTime())
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    upstream_links: Mapped[list["DeviceLink"]] = relationship(
        foreign_keys="DeviceLink.downstream_id", back_populates="downstream", cascade="all, delete-orphan"
    )


class DeviceLink(Base):
    """Topology edge: ``downstream`` depends on ``upstream`` for connectivity."""

    __tablename__ = "device_links"

    upstream_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="primary")  # primary|backup

    downstream: Mapped[Device] = relationship(foreign_keys=[downstream_id], back_populates="upstream_links")


class Metric(Base):
    __tablename__ = "metrics"

    time: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_metrics_device_metric_time", "device_id", "metric", "time"),)


class SyslogEvent(Base):
    __tablename__ = "syslog_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    time: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True, default=utcnow, index=True)
    device_id: Mapped[str | None] = mapped_column(String(32), index=True)
    host: Mapped[str] = mapped_column(String(128))
    severity: Mapped[int] = mapped_column(Integer)  # RFC5424 0=emerg .. 7=debug
    facility: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(32), default="system")  # auth|config|interface|system|security
    source_ip: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)


class FlowRecord(Base):
    __tablename__ = "flow_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    time: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True, default=utcnow, index=True)
    device_id: Mapped[str | None] = mapped_column(String(32))
    src_ip: Mapped[str] = mapped_column(String(64))
    dst_ip: Mapped[str] = mapped_column(String(64))
    src_port: Mapped[int] = mapped_column(Integer, default=0)
    dst_port: Mapped[int] = mapped_column(Integer, default=0)
    protocol: Mapped[int] = mapped_column(Integer, default=6)
    bytes: Mapped[int] = mapped_column(Integer)
    packets: Mapped[int] = mapped_column(Integer)


class Baseline(Base):
    """Running mean/variance (Welford) per device, metric and hour-of-week bucket.

    ``bucket`` is 0..167 for hour-of-week, or -1 for the global baseline.
    """

    __tablename__ = "baselines"

    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    mean: Mapped[float] = mapped_column(Float, default=0.0)
    m2: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(256))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|mitigating|resolved
    root_cause_device_id: Mapped[str | None] = mapped_column(String(32))
    rca: Mapped[dict] = mapped_column(JSON, default=dict)
    prevented: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    device_id: Mapped[str | None] = mapped_column(String(32), index=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # anomaly|threshold|prediction|security|availability
    metric: Mapped[str | None] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # info|warning|critical
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|acknowledged|resolved
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    last_seen: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    metric: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # exhaustion|saturation|failure
    probability: Mapped[float] = mapped_column(Float)
    eta: Mapped[datetime | None] = mapped_column(TZDateTime())
    summary: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class PlaybookRun(Base):
    __tablename__ = "playbook_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    playbook: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    alert_id: Mapped[str | None] = mapped_column(String(32))
    # pending_approval|approved|running|succeeded|failed|rolled_back|escalated|rejected|blocked
    status: Mapped[str] = mapped_column(String(24), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    driver: Mapped[str | None] = mapped_column(String(16))
    gates: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    approved_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class AuditLog(Base):
    """Append-only, hash-chained audit trail (each row commits to the previous)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
