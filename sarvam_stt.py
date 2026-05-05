"""
sarvam_stt.py — Custom Pipecat STT service powered by Sarvam AI (saaras:v3).

Sarvam's STT is batch (not streaming): we send the accumulated audio buffer
as a WAV file and get back a transcription.

Flow:
  1. Accumulate PCM audio from the pipeline
  2. Convert to WAV (16-bit, sample_rate)
  3. POST to Sarvam API
  4. Yield TranscriptionFrame with the result
"""

import asyncio
import io
import os
import wave
from typing import AsyncGenerator

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TranscriptionFrame,
)
from pipecat.services.stt_service import STTService


class SarvamSTTService(STTService):
    """
    Pipecat STT service using Sarvam AI speech-to-text.

    Parameters
    ----------
    api_key : str
        Sarvam API subscription key.
    language : str
        BCP-47 code, e.g. "bn-IN".
    model : str
        Sarvam STT model — "saaras:v3" (recommended) or "saarika:v2.5".
    sample_rate : int
        Audio sample rate to encode the WAV at.
    """

    API_URL = "https://api.sarvam.ai/speech-to-text"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        language: str = "bn-IN",
        model: str = "saaras:v3",
        sample_rate: int = 8000,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._api_key = api_key or os.getenv("SARVAM_API_KEY", "")
        if not self._api_key:
            raise ValueError("SarvamSTTService: SARVAM_API_KEY is not set")
        self._language = language
        self._model = model
        self._sample_rate = sample_rate
        logger.info(f"SarvamSTTService init — model={model}, language={language}, sr={sample_rate}")

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, sample_rate: int, num_channels: int = 1) -> bytes:
        """Wrap raw 16-bit PCM in a WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """
        Called by Pipecat with accumulated audio buffer.
        Sends audio to Sarvam and yields TranscriptionFrame.
        """
        if not audio or len(audio) < 160:  # skip empty / < 20ms of audio
            return

        logger.info(f"[SarvamSTT] processing {len(audio)} bytes of audio")

        try:
            wav_data = await asyncio.get_event_loop().run_in_executor(
                None, self._pcm_to_wav, audio, self._sample_rate
            )

            form = aiohttp.FormData()
            form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
            form.add_field("model", self._model)
            form.add_field("language_code", self._language)

            headers = {"api-subscription-key": self._api_key}

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.API_URL, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[SarvamSTT] HTTP {resp.status}: {body[:200]}")
                        yield ErrorFrame(error=f"Sarvam STT HTTP {resp.status}")
                        return

                    result = await resp.json()
                    transcript = result.get("transcript", "").strip()

                    if transcript:
                        logger.info(f"[SarvamSTT] transcript: {transcript!r}")
                        yield TranscriptionFrame(
                            text=transcript,
                            user_id="",
                            timestamp="",
                        )
                    else:
                        logger.debug("[SarvamSTT] empty transcript (silence or unrecognized)")

        except Exception as exc:
            logger.exception(f"[SarvamSTT] error: {exc}")
            yield ErrorFrame(error=f"Sarvam STT error: {exc}")
