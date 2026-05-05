"""
MongoDB call record persistence — optional, graceful.

If MONGO_URI is not set, every operation is a silent no-op.
Write failures never block or crash the pipeline.

Environment variables:
    MONGO_URI   — MongoDB connection string (e.g. mongodb+srv://...)
    MONGO_DB    — Database name (default: "voice_agent")
"""

import asyncio
import os
from typing import Any

from loguru import logger

_col = None
_init_attempted = False


def _get_collection():
    global _col, _init_attempted
    if _init_attempted:
        return _col
    _init_attempted = True

    uri = os.getenv("MONGO_URI", "")
    if not uri:
        logger.info("[DB] MONGO_URI not set — call records will not be persisted")
        return None

    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        db_name = os.getenv("MONGO_DB", "voice_agent")
        _col = client[db_name]["calls"]
        # Ping to catch bad credentials early
        client.admin.command("ping")
        logger.info(f"[DB] MongoDB connected — {db_name}.calls")
    except Exception as exc:
        logger.warning(f"[DB] MongoDB unavailable: {exc} — calls will not be persisted")
        _col = None

    return _col


async def save_call(data: dict[str, Any]) -> None:
    """Upsert the full call record. Fire-and-forget — never awaited by caller."""
    asyncio.create_task(_upsert(data))


async def update_call(call_sid: str, updates: dict[str, Any]) -> None:
    """Patch specific fields on an existing record. Fire-and-forget."""
    asyncio.create_task(_patch(call_sid, updates))


async def _upsert(data: dict[str, Any]) -> None:
    try:
        col = _get_collection()
        if col is None:
            return
        loop = asyncio.get_event_loop()
        call_sid = data.get("call_sid", "unknown")
        await loop.run_in_executor(
            None,
            lambda: col.update_one(
                {"call_sid": call_sid},
                {"$set": data},
                upsert=True,
            ),
        )
        logger.debug(f"[DB] saved call {call_sid}")
    except Exception as exc:
        logger.warning(f"[DB] save failed: {exc}")


async def _patch(call_sid: str, updates: dict[str, Any]) -> None:
    try:
        col = _get_collection()
        if col is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: col.update_one({"call_sid": call_sid}, {"$set": updates}),
        )
        logger.debug(f"[DB] updated call {call_sid}")
    except Exception as exc:
        logger.warning(f"[DB] update failed: {exc}")
