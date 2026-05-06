"""
AI Logger — logs LLM output so you can see what the AI generates.
"""

import time

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class AILogger(FrameProcessor):
    """Collects and logs LLM responses as they stream and in full."""

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._t0: float = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buf = []
            self._t0 = time.monotonic()
            print(f"\n{'*'*60}")
            print(f"🤖 [LLM] Generating response...")

        elif isinstance(frame, LLMTextFrame):
            text = getattr(frame, "text", "")
            if text:
                self._buf.append(text)
                print(text, end="", flush=True)

        elif isinstance(frame, LLMFullResponseEndFrame):
            full = "".join(self._buf).strip()
            elapsed = time.monotonic() - self._t0
            print()  # newline after streamed tokens
            if full:
                print(f"📝 [LLM] FULL RESPONSE ({elapsed:.2f}s): {full!r}")
                print(f"{'*'*60}\n")
                logger.info(f"[LLM] {full!r}  ({elapsed:.2f}s)")
            else:
                print(f"⚠️  [LLM] Empty response ({elapsed:.2f}s)")
                print(f"{'*'*60}\n")

        elif isinstance(frame, TextFrame):
            text = getattr(frame, "text", "").strip()
            if text:
                print(f"🗣️  [LLM→TTS] Sending to speech: {text!r}")
                logger.debug(f"[LLM→TTS] {text!r}")

        await self.push_frame(frame, direction)
