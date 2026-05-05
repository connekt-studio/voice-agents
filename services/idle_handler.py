"""
Caller silence detection with progressive retry prompts.

After IDLE_TIMEOUT_S of silence, prompt the caller.
After MAX_RETRIES silent periods, end the call gracefully.

Usage:
    handler = IdleHandler(pipeline_task)
    handler.arm()     # start the timer after bot finishes speaking
    handler.reset()   # cancel timer when caller speaks
"""

import asyncio

from loguru import logger


MAX_RETRIES = 3
IDLE_TIMEOUT_S = 8.0

# Progressive prompts: each silence gets a more direct message
_PROMPTS = [
    "আপনি কি আছেন?",
    "শুনতে পাচ্ছেন?",
    "সংযোগ বিচ্ছিন্ন মনে হচ্ছে। কল শেষ করছি। ধন্যবাদ।",
]


class IdleHandler:
    def __init__(self, task=None):
        self._task = task
        self._retry_count = 0
        self._timer: asyncio.Task | None = None

    def set_task(self, task) -> None:
        """Set the pipeline task after it's created (avoids circular dependency)."""
        self._task = task

    def arm(self) -> None:
        """Start (or restart) the idle timer. Call after bot finishes speaking."""
        self._cancel_timer()
        self._timer = asyncio.create_task(self._countdown())

    def reset(self) -> None:
        """Cancel any pending idle timer. Call when the caller starts speaking."""
        self._cancel_timer()
        self._retry_count = 0

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _countdown(self) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT_S)
            await self._on_idle()
        except asyncio.CancelledError:
            pass

    async def _on_idle(self) -> None:
        if self._retry_count >= MAX_RETRIES:
            logger.warning("[IdleHandler] max retries reached — cancelling pipeline")
            await self._task.cancel()
            return

        prompt = _PROMPTS[min(self._retry_count, len(_PROMPTS) - 1)]
        self._retry_count += 1
        logger.info(f"[IdleHandler] silence retry {self._retry_count}/{MAX_RETRIES}: {prompt!r}")

        from pipecat.frames.frames import TTSSpeakFrame
        await self._task.queue_frames([TTSSpeakFrame(text=prompt)])

        # Re-arm for the next silence window
        self.arm()
