"""Standalone worker: collectors + intelligence + automation (no HTTP).

Usage: ``python -m app.worker`` (requires ``NG_REDIS_URL`` so the API can
reach it through the event/command channels).
"""

import asyncio
import logging
import signal

from app.services.runtime import Runtime, set_runtime


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = Runtime()
    set_runtime(runtime)
    await runtime.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
