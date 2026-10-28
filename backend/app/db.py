"""Database engine/session management with optional TimescaleDB setup."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()
_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=not _is_sqlite,
    connect_args={"timeout": 30} if _is_sqlite else {},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def startup_lock():
    """Serialise schema creation + seeding when API and worker start together."""
    if engine.dialect.name != "postgresql":
        yield
        return
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(724002)"))
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(724002)"))
            await conn.commit()


async def init_db() -> None:
    """Create tables and, on PostgreSQL with TimescaleDB, hypertables + policies."""
    from app import models  # noqa: F401  (register models)

    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)

    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        has_ts = (await conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"))).scalar()
        if not has_ts:
            log.warning("TimescaleDB extension not available; using plain PostgreSQL tables")
            return
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        for table in ("metrics", "flow_records", "syslog_events"):
            await conn.execute(
                text(f"SELECT create_hypertable('{table}', 'time', if_not_exists => TRUE, migrate_data => TRUE)")
            )
            await conn.execute(
                text(f"SELECT add_retention_policy('{table}', INTERVAL '90 days', if_not_exists => TRUE)")
            )
        await conn.execute(
            text("ALTER TABLE metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id, metric')")
        )
        await conn.execute(text("SELECT add_compression_policy('metrics', INTERVAL '7 days', if_not_exists => TRUE)"))
        log.info("TimescaleDB hypertables, compression and retention policies configured")
