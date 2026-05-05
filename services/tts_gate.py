"""
TTS Gate Processor — watches for TTS stop events and fires callbacks.

Used to arm the idle handler only AFTER the AI finishes speaking,
rather than when speech is merely queued.
"""

from typing import Callable

from pipecat.frames.frames import Frame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class TTSGateProcessor(FrameProcessor):
    """
    Watches for TTS stop frames and fires on_tts_finished callback.

    Does NOT toggle audio_in — use allow_interruptions=False in
    PipelineParams instead, which prevents user speech from
    interrupting the bot at the framework level.
    """

    def __init__(self, on_tts_finished: Callable):
        super().__init__()
        self._on_tts_finished = on_tts_finished

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStoppedFrame):
            logger.debug("[TTSGate] TTS stopped — firing callback")
            self._on_tts_finished()

        await self.push_frame(frame, direction)
