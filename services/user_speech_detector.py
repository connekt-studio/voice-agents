"""
User Speech Detector — resets the idle handler when the user starts speaking.

Watches for STT transcription frames and triggers idle_handler.reset()
so the silence timer doesn't disconnect the call while the user is talking.
"""

from typing import Callable

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class UserSpeechDetector(FrameProcessor):
    """
    Watches for STT transcription frames and resets the idle timer
    whenever the user speaks.
    """

    def __init__(self, on_user_speech: Callable):
        super().__init__()
        self._on_user_speech = on_user_speech

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            logger.debug("[UserSpeechDetector] user speech detected — resetting idle timer")
            self._on_user_speech()

        await self.push_frame(frame, direction)
