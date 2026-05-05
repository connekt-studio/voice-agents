"""
Slack call notifications — optional, graceful.

If SLACK_BOT_TOKEN or SLACK_CHANNEL is not set, every operation is a silent no-op.
Slack failures never block or crash the pipeline.

Environment variables:
    SLACK_BOT_TOKEN — Slack bot OAuth token (xoxb-...)
    SLACK_CHANNEL   — Channel ID or name to post into (e.g. #voice-calls)
"""

import asyncio
import os
from typing import Optional

from loguru import logger

_client = None
_client_init = False


def _get_client():
    global _client, _client_init
    if _client_init:
        return _client
    _client_init = True

    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        logger.info("[Slack] SLACK_BOT_TOKEN not set — notifications disabled")
        return None

    try:
        from slack_sdk.async_client import AsyncWebClient
        _client = AsyncWebClient(token=token)
        logger.info("[Slack] client ready")
    except ImportError:
        logger.warning("[Slack] slack_sdk not installed — pip install slack_sdk")
    except Exception as exc:
        logger.warning(f"[Slack] init error: {exc}")

    return _client


def _channel() -> str:
    return os.getenv("SLACK_CHANNEL", "")


async def post_call_started(call_sid: str, caller: str, call_type: str) -> None:
    text = (
        f":phone: *New call* — `{call_sid}`\n"
        f"Caller: `{caller}` | Type: `{call_type}`"
    )
    asyncio.create_task(_post(text))


async def post_call_ended(
    call_sid: str,
    status: str,
    transcript_lines: list[str],
    error_counters: Optional[dict] = None,
) -> None:
    last_turns = transcript_lines[-10:]  # cap to last 10 turns
    transcript_block = "\n".join(f"> {line}" for line in last_turns) if last_turns else "_no transcript_"

    errors = ""
    if error_counters and any(v > 0 for v in error_counters.values()):
        errors = "\nErrors: " + ", ".join(f"{k}={v}" for k, v in error_counters.items() if v > 0)

    text = (
        f":end: *Call ended* — `{call_sid}` | Status: `{status}`{errors}\n"
        f"{transcript_block}"
    )
    asyncio.create_task(_post(text))


async def _post(text: str) -> None:
    client = _get_client()
    channel = _channel()
    if not client or not channel:
        return
    try:
        await client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        logger.warning(f"[Slack] post failed: {exc}")
