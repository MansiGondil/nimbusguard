"""Network health score (0-100) shown on the executive dashboard."""

PENALTY = {"info": 0.5, "warning": 3.0, "critical": 8.0}


def device_health(values: dict[str, float] | None, status: str, alerts: list[str]) -> float:
    """Health of one device from its status, open alert severities and headroom."""
    if status == "down" or values is None:
        return 0.0
    score = 100.0
    score -= sum(PENALTY.get(s, 0) for s in alerts)
    # Gentle headroom penalty as utilisation metrics approach their ceiling.
    for metric in ("cpu", "memory", "disk"):
        v = values.get(metric)
        if v is not None and v > 70:
            score -= (v - 70) * 0.3
    if (loss := values.get("packet_loss")) is not None:
        score -= min(20.0, loss * 2)
    return max(0.0, min(100.0, score))


WEIGHT = {"low": 0.5, "medium": 1.0, "high": 1.5, "critical": 2.0}


def network_health(devices: list[dict]) -> float:
    """Criticality-weighted mean of device health.

    ``devices`` items: {"criticality", "status", "values", "alerts": [severity, ...]}
    """
    if not devices:
        return 100.0
    total = weight = 0.0
    for d in devices:
        w = WEIGHT.get(d.get("criticality", "medium"), 1.0)
        total += w * device_health(d.get("values"), d.get("status", "up"), d.get("alerts", []))
        weight += w
    return round(total / weight, 1)
