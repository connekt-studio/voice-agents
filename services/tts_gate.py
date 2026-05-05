"""
TTS Gate Processor — disables audio input while AI is speaking.

Prevents the bot from receiving user audio during its own TTS playback,
so the AI speaks first without interruption, then listens for user input.
"""

from pipecat.frames.frames import Frame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class TTSGateProcessor(FrameProcessor):
    """
    Watches for TTS start/stop frames and toggles audio input on the transport.

    - TTSStartedFrame  → disables audio_in
    - TTSStoppedFrame  → enables audio_in
    """

    def __init__(self, transport):
        super().__init__()
        self._transport = transport
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

        await self.push_frame(frame, direction)
