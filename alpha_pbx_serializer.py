"""
alpha_pbx_serializer.py — Raw μ-law WebSocket serializer for Alpha PBX SIP bridge.

Alpha PBX (connektstudio.alphapbx.net) sends raw binary μ-law (PCMU) audio
frames over the WebSocket — NOT Twilio-format JSON.  This serializer handles
the raw binary ↔ PCM conversion without any Twilio dependency.

Pipecat 0.0.99 API note:
  pcm_to_ulaw(pcm_bytes, in_rate, out_rate, resampler)
  ulaw_to_pcm(ulaw_bytes, in_rate, out_rate, resampler)
  Both require a BaseAudioResampler instance as the 4th argument.
  Since SIP audio is already 8 kHz, in_rate == out_rate == 8000 (no-op resample).
"""

from pipecat.audio.resamplers.soxr_resampler import SOXRAudioResampler
from pipecat.audio.utils import pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import AudioRawFrame, Frame, InputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer

# One shared resampler instance (8 kHz → 8 kHz — effectively a no-op)
_resampler = SOXRAudioResampler()


class AlphaPBXSerializer(FrameSerializer):
    """
    Raw binary μ-law serializer for Alpha PBX SIP bridges.

    - Incoming (WebSocket → pipeline): raw bytes → ulaw_to_pcm → InputAudioRawFrame
    - Outgoing (pipeline → WebSocket): AudioRawFrame → pcm_to_ulaw → raw bytes
    """

    def __init__(self, sample_rate: int = 8000, num_channels: int = 1):
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def serialize(self, frame: Frame) -> bytes | None:
        """Convert PCM audio frame to raw μ-law bytes for the SIP bridge."""
        if not isinstance(frame, AudioRawFrame):
            return None
        ulaw_bytes = await pcm_to_ulaw(
            frame.audio,
            frame.sample_rate,      # in_rate  (usually 8000 or 16000 from TTS)
            self._sample_rate,      # out_rate (8000 for SIP μ-law)
            _resampler,
        )
        return ulaw_bytes

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Convert raw μ-law bytes from the SIP bridge to a PCM audio frame."""
        if not isinstance(data, bytes) or len(data) == 0:
            return None
        pcm_bytes = await ulaw_to_pcm(
            data,
            self._sample_rate,      # in_rate  (8000 from SIP)
            self._sample_rate,      # out_rate (8000 — same, no resampling)
            _resampler,
        )
        return InputAudioRawFrame(
            audio=pcm_bytes,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
