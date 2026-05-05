"""
AI Logger — logs LLM output so you can see what the AI generates.
"""

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class AILogger(FrameProcessor):
    """Collects and logs LLM responses in full."""

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buf = []

        elif isinstance(frame, LLMTextFrame):
            text = getattr(frame, "text", "")
            if text:
                self._buf.append(text)

        elif isinstance(frame, LLMFullResponseEndFrame):
            full = "".join(self._buf).strip()
            if full:
                logger.info(f"[🤖 AI Response] {full!r}")

        await self.push_frame(frame, direction)
