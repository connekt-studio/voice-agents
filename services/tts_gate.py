"""
TTS Gate Processor — prevents audio echo loops and arms idle handler.

Switches VAD profiles when the bot is speaking to suppress TTS echo
from being detected as user speech, which causes the AI to repeat itself.
"""

from typing import Callable

from pipecat.frames.frames import Frame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class TTSGateProcessor(FrameProcessor):
    """
    Watches for TTS start/stop frames and switches VAD profiles:

    - TTSStartedFrame  → VAD "agent_turn" (high threshold, filters echo)
    - TTSStoppedFrame  → VAD "normal" + fires on_tts_finished callback
    """

    def __init__(self, vad_service, on_tts_finished: Callable):
        super().__init__()
        self._vad = vad_service
        self._on_tts_finished = on_tts_finished

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            await self._vad.set_profile("agent_turn")

        elif isinstance(frame, TTSStoppedFrame):
            await self._vad.set_profile("normal")
            self._on_tts_finished()

        await self.push_frame(frame, direction)
