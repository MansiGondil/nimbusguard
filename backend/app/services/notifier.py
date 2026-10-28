"""Notifications: in-app (WebSocket), mobile push via ntfy, and webhooks.

ntfy (https://ntfy.sh) is open source with Android/iOS apps, so users
get push notifications on their phones by subscribing to a topic - no app
store deployment required. Self-host it by setting ``NG_NTFY_SERVER``.
"""

import logging

import httpx

from app.cache import get_cache
from app.config import get_settings

log = logging.getLogger(__name__)
PRIORITY = {"low": "2", "default": "3", "high": "4", "urgent": "5"}


async def notify(title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> None:
    settings = get_settings()
    await get_cache().publish({"type": "notification", "title": title, "message": message, "priority": priority})
    if not (settings.ntfy_topic or settings.webhook_url):
        return
    async with httpx.AsyncClient(timeout=5) as client:
        if settings.ntfy_topic:
            try:
                await client.post(
                    f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}",
                    content=message.encode(),
                    headers={"Title": title, "Priority": PRIORITY.get(priority, "3"), "Tags": ",".join(tags or [])},
                )
            except httpx.HTTPError as exc:
                log.warning("ntfy push failed: %s", exc)
        if settings.webhook_url:
            try:
                # Slack/Teams/Discord-compatible "text" field plus structured fields.
                await client.post(
                    settings.webhook_url,
                    json={"text": f"*{title}*\n{message}", "title": title, "message": message, "priority": priority},
                )
            except httpx.HTTPError as exc:
                log.warning("Webhook notification failed: %s", exc)
