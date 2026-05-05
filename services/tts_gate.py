"""
TTS Gate Processor — disables audio input while AI is speaking.

Prevents the bot from receiving user audio during its own TTS playback,
so the AI speaks first without interruption, then listens for user input.
"""

from typing import Callable

from pipecat.frames.frames import Frame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class TTSGateProcessor(FrameProcessor):
    """
    Watches for TTS start/stop frames and toggles audio input on the transport.

    - TTSStartedFrame  → disables audio_in
    - TTSStoppedFrame  → enables audio_in, fires on_tts_finished callback
    """

    def __init__(self, transport, on_tts_finished: Callable | None = None):
        super().__init__()
        self._transport = transport
        self._on_tts_finished = on_tts_finished
        self._speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            if not self._speaking:
                self._speaking = True
                logger.debug("[TTSGate] TTS started — disabling audio input")
                self._transport.params.audio_in_enabled = False

        elif isinstance(frame, TTSStoppedFrame):
            if self._speaking:
                self._speaking = False
                logger.debug("[TTSGate] TTS stopped — enabling audio input")
                self._transport.params.audio_in_enabled = True
                if self._on_tts_finished:
                    await self._on_tts_finished()

        await self.push_frame(frame, direction)
