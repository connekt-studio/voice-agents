"""
alpha_pbx_serializer.py — JSON μ-law WebSocket serializer for Alpha PBX SIP bridge.

The SIP bridge in sip_outbound.py uses Twilio-compatible JSON framing:
  Inbound  (bridge → bot): {"event":"media","media":{"payload":"<base64-mulaw>"}}
  Outbound (bot → bridge): {"event":"media","media":{"payload":"<base64-mulaw>"}}

This serializer translates between that JSON wire format and Pipecat's
internal PCM AudioRawFrame so the two ends stay in sync.

Audio chain:
  Caller voice → SIP RTP (PCMU μ-law 8 kHz)
    → sip_to_ws() encodes as base64 in JSON → WebSocket → here
    → deserialize(): base64 decode → ulaw_to_pcm → InputAudioRawFrame → STT

  TTS PCM → serialize(): pcm_to_ulaw → base64 → JSON → WebSocket
    → ws_to_sip() decodes → call.writeAudio(pcm) → SIP RTP → caller's ear
"""

import base64
import json

from pipecat.audio.resamplers.soxr_resampler import SOXRAudioResampler
from pipecat.audio.utils import pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import AudioRawFrame, Frame, InputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer

# One shared resampler (8 kHz → 8 kHz — effectively a no-op for SIP audio)
_resampler = SOXRAudioResampler()


class AlphaPBXSerializer(FrameSerializer):
    """
    Twilio-compatible JSON μ-law serializer for the Alpha PBX SIP bridge.

    Inbound  (WebSocket JSON → pipeline PCM):
        {"event":"media","media":{"payload":"<base64 μ-law>"}}
        → base64-decode → ulaw_to_pcm → InputAudioRawFrame

    Outbound (pipeline PCM → WebSocket JSON):
        AudioRawFrame
        → pcm_to_ulaw → base64-encode
        → {"event":"media","media":{"payload":"<base64 μ-law>"}}
    """

    def __init__(self, sample_rate: int = 8000, num_channels: int = 1):
        self._sample_rate  = sample_rate
        self._num_channels = num_channels

    # ── Outbound: bot audio → JSON for the SIP bridge ─────────────────────────
    async def serialize(self, frame: Frame) -> str | None:
        """
        Convert a Pipecat AudioRawFrame (PCM) to Twilio-compatible JSON
        so that ws_to_sip() in sip_outbound.py can decode it and feed RTP.
        """
        if not isinstance(frame, AudioRawFrame):
            return None

        ulaw_bytes = await pcm_to_ulaw(
            frame.audio,
            frame.sample_rate,   # in_rate  (TTS may output 8 kHz or 16 kHz)
            self._sample_rate,   # out_rate (8 kHz for SIP μ-law)
            _resampler,
        )

        return json.dumps({
            "event": "media",
            "media": {
                "payload": base64.b64encode(ulaw_bytes).decode("ascii"),
            },
        })

    # ── Inbound: JSON from SIP bridge → bot audio ─────────────────────────────
    async def deserialize(self, data: str | bytes) -> Frame | None:
        """
        Convert Twilio-compatible JSON from sip_to_ws() into a PCM frame
        that Pipecat's STT service can process.

        Ignores non-media events (connected, start, stop, etc.).
        """
        # Normalise to str
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8")
            except Exception:
                return None

        if not isinstance(data, str) or not data.strip():
            return None

        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return None

        event = msg.get("event", "")

        # Handshake / control frames — let the pipeline ignore them
        if event in ("connected", "start", "stop", "mark", "disconnect", ""):
            return None

        if event != "media":
            return None

        payload_b64 = msg.get("media", {}).get("payload", "")
        if not payload_b64:
            return None

        try:
            mulaw_bytes = base64.b64decode(payload_b64)
        except Exception:
            return None

        if len(mulaw_bytes) == 0:
            return None

        pcm_bytes = await ulaw_to_pcm(
            mulaw_bytes,
            self._sample_rate,   # in_rate  (8 kHz from SIP)
            self._sample_rate,   # out_rate (8 kHz — same, no resampling)
            _resampler,
        )

        return InputAudioRawFrame(
            audio=pcm_bytes,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
