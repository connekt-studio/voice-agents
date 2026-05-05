"""
Fire-and-forget structured call event logger.

Uses asyncio.create_task() so observability never blocks the audio pipeline.
Extend _persist() to write to MongoDB, Axiom, CloudWatch, etc.
"""

import asyncio
from datetime import datetime
from typing import Any

from loguru import logger


def fire_event(call_sid: str, event_type: str, metadata: dict[str, Any] | None = None) -> None:
    """
    Emit a structured call event without blocking.
    Safe to call from any async context — wraps in create_task().
    """
    try:
        asyncio.create_task(_emit(call_sid, event_type, metadata or {}))
    except RuntimeError:
        # No running event loop (e.g. called from sync test code)
        logger.warning(f"[event_logger] no running loop — event dropped: {event_type}")


async def _emit(call_sid: str, event_type: str, metadata: dict[str, Any]) -> None:
    entry = {
        "call_sid": call_sid,
        "event": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        **metadata,
    }
    logger.info(f"[EVENT] {entry}")
    await _persist(entry)


async def _persist(entry: dict[str, Any]) -> None:
    """
    Placeholder for external persistence (MongoDB, Axiom, etc.).
    Import and call db.log_event(entry) here when ready.
    """
    pass
