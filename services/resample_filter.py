"""
Resample Filter — converts TTS audio from one sample rate to another.

Gemini TTS outputs at 24000 Hz but our telephony pipeline needs 8000 Hz.
This frame processor handles the downsampling.
"""

import audioop

from pipecat.frames.frames import Frame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class ResampleFilter(FrameProcessor):
    """
    Resamples TTSAudioRawFrame from input_rate to output_rate.

    Uses audioop.ratecv for integer-ratio resampling (e.g., 24000 → 8000 = 3:1).
    """

    def __init__(self, input_rate: int, output_rate: int):
        super().__init__()
        self._input_rate = input_rate
        self._output_rate = output_rate
        self._state = None
        logger.info(f"ResampleFilter: {input_rate} Hz → {output_rate} Hz")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSAudioRawFrame) and frame.sample_rate == self._input_rate:
            pcm = frame.audio
            num_channels = frame.num_channels or 1
            sample_width = 2  # 16-bit PCM

            try:
                resampled, self._state = audioop.ratecv(
                    pcm, sample_width, num_channels,
                    self._input_rate, self._output_rate, self._state
                )
                frame = TTSAudioRawFrame(
                    audio=resampled,
                    sample_rate=self._output_rate,
                    num_channels=num_channels,
                )
            except Exception as exc:
                logger.warning(f"[ResampleFilter] error: {exc}")

        await self.push_frame(frame, direction)
