"""
Resample Filter — converts TTS audio from one sample rate to another.

Gemini TTS outputs at 24000 Hz but our telephony pipeline needs 8000 Hz.
Uses SOXRStreamAudioResampler (VHQ soxr) with state held for the entire call,
avoiding the 200 ms auto-clear that the transport's built-in resampler applies.
"""

from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import Frame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from loguru import logger


class ResampleFilter(FrameProcessor):
    """
    Resamples TTSAudioRawFrame from input_rate to output_rate using soxr VHQ.
    Holds resampler state for the full call to avoid inter-chunk artifacts.
    """

    def __init__(self, input_rate: int, output_rate: int):
        super().__init__()
        self._input_rate = input_rate
        self._output_rate = output_rate
        self._resampler = SOXRStreamAudioResampler()
        logger.info(f"ResampleFilter: {input_rate} Hz → {output_rate} Hz (soxr VHQ)")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSAudioRawFrame) and frame.sample_rate == self._input_rate:
            try:
                resampled = await self._resampler.resample(
                    frame.audio, self._input_rate, self._output_rate
                )
                frame = TTSAudioRawFrame(
                    audio=resampled,
                    sample_rate=self._output_rate,
                    num_channels=frame.num_channels or 1,
                )
            except Exception as exc:
                logger.warning(f"[ResampleFilter] error: {exc}")

        await self.push_frame(frame, direction)
