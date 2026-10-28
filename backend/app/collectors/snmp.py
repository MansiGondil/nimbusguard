"""SNMP collector (v2c) built on pysnmp's asyncio API.

Polls a vendor profile of OIDs per device and converts raw counters into
rates. The OID fetching is isolated behind ``fetch`` so the conversion logic
can be unit-tested without a live agent.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.collectors.base import Sample

log = logging.getLogger(__name__)

# Vendor profiles: metric -> OID. Counters (``*_octets``/``*_errors``) are
# converted to per-second / per-minute rates between polls.
PROFILES: dict[str, dict[str, str]] = {
    "net-snmp": {
        "cpu_idle": "1.3.6.1.4.1.2021.11.11.0",  # UCD-SNMP-MIB::ssCpuIdle
        "mem_total": "1.3.6.1.4.1.2021.4.5.0",  # memTotalReal (kB)
        "mem_avail": "1.3.6.1.4.1.2021.4.6.0",  # memAvailReal (kB)
        "in_octets": "1.3.6.1.2.1.31.1.1.1.6.2",  # IF-MIB::ifHCInOctets.2
        "out_octets": "1.3.6.1.2.1.31.1.1.1.10.2",  # IF-MIB::ifHCOutOctets.2
        "in_errors": "1.3.6.1.2.1.2.2.1.14.2",  # IF-MIB::ifInErrors.2
    },
    "cisco-ios": {
        "cpu": "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1",  # cpmCPUTotal5minRev
        "mem_used": "1.3.6.1.4.1.9.9.48.1.1.1.5.1",  # ciscoMemoryPoolUsed
        "mem_free": "1.3.6.1.4.1.9.9.48.1.1.1.6.1",  # ciscoMemoryPoolFree
        "in_octets": "1.3.6.1.2.1.31.1.1.1.6.1",
        "out_octets": "1.3.6.1.2.1.31.1.1.1.10.1",
        "in_errors": "1.3.6.1.2.1.2.2.1.14.1",
        "temperature": "1.3.6.1.4.1.9.9.13.1.3.1.3.1",  # ciscoEnvMonTemperatureStatusValue
    },
}

Fetcher = Callable[[str, int, str, list[str]], Awaitable[dict[str, float]]]


async def pysnmp_fetch(host: str, port: int, community: str, oids: list[str]) -> dict[str, float]:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
    )

    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((host, port), timeout=2, retries=1)
        err_ind, err_status, _idx, var_binds = await get_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            *[ObjectType(ObjectIdentity(o)) for o in oids],
        )
        if err_ind or err_status:
            raise ConnectionError(str(err_ind or err_status.prettyPrint()))
        out = {}
        for name, val in var_binds:
            try:
                out[str(name)] = float(val)
            except (TypeError, ValueError):
                continue
        return out
    finally:
        engine.close_dispatcher()


class SnmpPoller:
    def __init__(self, fetch: Fetcher = pysnmp_fetch) -> None:
        self.fetch = fetch
        self._prev: dict[str, tuple[float, dict[str, float]]] = {}

    def convert(self, device_id: str, raw: dict[str, float], capacity_mbps: float, now: float) -> dict[str, float]:
        values: dict[str, float] = {}
        if "cpu_idle" in raw:
            values["cpu"] = 100.0 - raw["cpu_idle"]
        elif "cpu" in raw:
            values["cpu"] = raw["cpu"]
        if "mem_total" in raw and raw["mem_total"]:
            values["memory"] = 100.0 * (1 - raw.get("mem_avail", 0) / raw["mem_total"])
        elif "mem_used" in raw and "mem_free" in raw:
            total = raw["mem_used"] + raw["mem_free"]
            values["memory"] = 100.0 * raw["mem_used"] / total if total else 0.0
        if "temperature" in raw:
            values["temperature"] = raw["temperature"]

        prev = self._prev.get(device_id)
        if prev:
            then, prev_raw = prev
            dt = max(now - then, 1e-6)

            def rate(key: str) -> float | None:
                if key not in raw or key not in prev_raw:
                    return None
                delta = raw[key] - prev_raw[key]
                if delta < 0:  # counter wrap/reset
                    return None
                return delta / dt

            if (r := rate("in_octets")) is not None:
                values["traffic_in"] = r * 8 / 1e6
            if (r := rate("out_octets")) is not None:
                values["traffic_out"] = r * 8 / 1e6
            if (r := rate("in_errors")) is not None:
                values["if_errors"] = r * 60
        self._prev[device_id] = (now, raw)
        return {k: round(v, 3) for k, v in values.items()}

    async def poll(
        self,
        device_id: str,
        host: str,
        community: str,
        profile: str = "net-snmp",
        capacity_mbps: float = 1000.0,
        port: int = 161,
    ) -> Sample | None:
        oids = PROFILES[profile]
        t0 = time.monotonic()
        try:
            by_oid = await self.fetch(host, port, community, list(oids.values()))
        except Exception as exc:
            log.warning("SNMP poll of %s (%s) failed: %s", device_id, host, exc)
            return None
        latency_ms = (time.monotonic() - t0) * 1000
        raw = {name: by_oid[oid] for name, oid in oids.items() if oid in by_oid}
        values = self.convert(device_id, raw, capacity_mbps, time.monotonic())
        values["latency"] = round(latency_ms, 2)
        return Sample(device_id, datetime.now(UTC), values)
