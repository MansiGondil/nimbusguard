"""NimbusGuard API entrypoint: ``uvicorn app.main:app``."""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import alerts, auth, dashboard, devices, remediation, system
from app.cache import get_cache
from app.config import get_settings
from app.db import init_db, startup_lock
from app.services.runtime import Runtime, set_runtime
from app.services.seed import seed

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nimbusguard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = None
    if settings.run_workers:
        runtime = Runtime()
        set_runtime(runtime)
        await runtime.start()
    else:
        async with startup_lock():
            await init_db()
            await seed()
    yield
    if runtime:
        await runtime.stop()
        set_runtime(None)
    await get_cache().close()


app = FastAPI(
    title="NimbusGuard API",
    version="1.0.0",
    description="Autonomous self-healing network platform - Predict. Prevent. Protect.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
    return response


for module in (auth, devices, alerts, remediation, dashboard, system):
    app.include_router(module.router, prefix="/api")
app.include_router(system.ws_router)


@app.get("/healthz", tags=["system"])
async def healthz():
    return {"status": "ok"}


# Serve the built React app when present (single-container deployment).
STATIC = Path(__file__).resolve().parent.parent / "static"
if STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        file = STATIC / path
        return FileResponse(file if path and file.is_file() else STATIC / "index.html")
