import os
import tempfile

# Configure a fast, isolated demo environment *before* the app is imported.
_tmp = tempfile.mkdtemp(prefix="nimbusguard-test-")
os.environ.update(
    {
        "NG_DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
        "NG_SIMULATOR_DEVICES": "8",
        "NG_SIMULATOR_BACKFILL_DAYS": "1",
        "NG_SIMULATOR_INTERVAL_SECONDS": "0.5",
        "NG_ANALYSIS_INTERVAL_SECONDS": "1",
        "NG_PERSIST_INTERVAL_SECONDS": "2",
        "NG_TIME_SCALE": "60",
        "NG_SIMULATOR_CHAOS_MINUTES": "0",
        "NG_SECRET_KEY": "test-secret-key-for-unit-tests-only",
    }
)
os.environ.pop("NG_REDIS_URL", None)

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402


@pytest.fixture
async def session():
    """A throwaway in-memory database for unit tests that need persistence."""
    from app import models  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()
