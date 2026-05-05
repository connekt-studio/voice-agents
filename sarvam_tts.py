"""
sarvam_tts.py — Custom Pipecat TTS service powered by Sarvam AI (bulbul:v3).

Sarvam streams MP3 audio back in chunks.  We:
  1. Collect the full MP3 blob in an async thread (requests is sync).
  2. Decode MP3 → PCM with pydub (uses ffmpeg under the hood).
  3. Resample to the pipeline's output sample-rate (default 8 000 Hz).
  4. Emit raw AudioRawFrame chunks to the Pipecat pipeline.

Requirements:
    pip install requests pydub
    # pydub needs ffmpeg on PATH — install via: brew install ffmpeg
"""

import asyncio
import io
import os
import time
from typing import AsyncGenerator

import requests
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService

# Retry config for transient Sarvam API errors
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5   # 0.5 → 1.0 → 2.0 seconds


# ──────────────────────────────────────────────────────────────────────────────
# Sarvam TTS Service
# ──────────────────────────────────────────────────────────────────────────────

class SarvamTTSService(TTSService):
    """
    Pipecat TTS service that synthesises Bangla speech via Sarvam AI.

    Parameters
    ----------
    api_key : str
        Your Sarvam API subscription key (SARVAM_API_KEY env var).
    speaker : str
        Sarvam voice name, e.g. "pooja", "meera", "arvind", "arjun" …
    language : str
        BCP-47 language code understood by Sarvam, e.g. "bn-IN".
    model : str
        Sarvam TTS model, e.g. "bulbul:v3".
    pace : float
        Speech speed multiplier (0.5 – 2.0, default 1.0).
    sample_rate : int
        Output PCM sample rate fed to the SIP bridge (8 000 Hz for telephony).
    """

    API_URL = "https://api.sarvam.ai/text-to-speech/stream"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        speaker: str = "pooja",
        language: str = "bn-BN",
        model: str = "bulbul:v3",
        pace: float = 1.0,
        sample_rate: int = 8000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)

        self._api_key = api_key or os.getenv("SARVAM_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "SarvamTTSService: SARVAM_API_KEY is not set. "
                "Add it to your .env file or pass api_key= explicitly."
            )

        self._speaker = speaker
        self._language = language
        self._model = model
        self._pace = pace
        self._out_sample_rate = sample_rate

        logger.info(
            f"SarvamTTSService init — model={model}, speaker={speaker}, "
            f"language={language}, pace={pace}, sample_rate={sample_rate} Hz"
        )

    # ------------------------------------------------------------------
    # Internal helper — runs in a thread-pool so we don't block the loop
    # ------------------------------------------------------------------

    def _call_sarvam_sync(self, text: str) -> bytes:
        """
        Blocking HTTP call that returns the full MP3 payload.
        Retries up to _MAX_RETRIES times with exponential backoff on transient errors.
        4xx errors are permanent and not retried.
        """
        headers = {
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "target_language_code": self._language,
            "speaker": self._speaker,
            "model": self._model,
            "pace": self._pace,
            "speech_sample_rate": 22050,   # Sarvam native; we resample later
            "output_audio_codec": "mp3",
            "enable_preprocessing": True,
        }

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.debug(f"[SarvamTTS] POST attempt {attempt}/{_MAX_RETRIES} | text={text[:60]!r}…")
                mp3_chunks: list[bytes] = []

                with requests.post(
                    self.API_URL,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=30,
                ) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            mp3_chunks.append(chunk)

                mp3_data = b"".join(mp3_chunks)
                logger.debug(f"[SarvamTTS] total MP3: {len(mp3_data)} bytes (attempt {attempt})")
                return mp3_data

            except requests.HTTPError as exc:
                # 4xx errors are permanent — don't retry
                if exc.response is not None and exc.response.status_code < 500:
                    raise
                last_exc = exc
                logger.warning(
                    f"[SarvamTTS] HTTP {exc.response.status_code} on attempt {attempt}, retrying…"
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(f"[SarvamTTS] network error on attempt {attempt}: {exc}, retrying…")

            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.debug(f"[SarvamTTS] backoff {delay:.1f}s")
                time.sleep(delay)

        raise last_exc or RuntimeError("SarvamTTS: all retries exhausted")

    # ------------------------------------------------------------------
    # MP3 → 8 kHz mono PCM16 conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _mp3_to_pcm(mp3_data: bytes, out_rate: int) -> bytes:
        """
        Decode MP3 with pydub and return raw little-endian 16-bit PCM
        at *out_rate* Hz, mono.
        """
        try:
            from pydub import AudioSegment  # imported lazily — optional dep
        except ImportError as exc:
            raise RuntimeError(
                "pydub is required for SarvamTTSService. "
                "Install it with: pip install pydub\n"
                "Also make sure ffmpeg is on your PATH (brew install ffmpeg)."
            ) from exc

        audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
        audio = audio.set_channels(1).set_frame_rate(out_rate).set_sample_width(2)
        return audio.raw_data

    # ------------------------------------------------------------------
    # Pipecat TTSService interface
    # ------------------------------------------------------------------

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        """
        Called by Pipecat for each TTS chunk.  Yields:
            TTSStartedFrame → TTSAudioRawFrame(s) → TTSStoppedFrame
        """
        logger.info(f"[SarvamTTS] run_tts: {text[:80]!r}")

        yield TTSStartedFrame()

        try:
            # Run the blocking HTTP call off the event-loop thread
            loop = asyncio.get_event_loop()
            mp3_data = await loop.run_in_executor(None, self._call_sarvam_sync, text)

            # Decode + resample
            pcm_data = await loop.run_in_executor(
                None, self._mp3_to_pcm, mp3_data, self._out_sample_rate
            )

            # Emit in 10 ms frames (80 samples × 2 bytes = 160 bytes at 8 kHz)
            # Smaller frames = faster initial playback, lower perceived latency
            frame_bytes = int(self._out_sample_rate * 0.01) * 2  # 160 bytes
            for offset in range(0, len(pcm_data), frame_bytes):
                chunk = pcm_data[offset : offset + frame_bytes]
                if chunk:
                    yield TTSAudioRawFrame(
                        audio=chunk,
                        sample_rate=self._out_sample_rate,
                        num_channels=1,
                    )

        except requests.HTTPError as exc:
            logger.error(f"[SarvamTTS] HTTP error: {exc.response.status_code} — {exc.response.text}")
            yield ErrorFrame(error=f"SarvamTTS HTTP error: {exc}")
        except Exception as exc:
            logger.exception(f"[SarvamTTS] unexpected error: {exc}")
            yield ErrorFrame(error=f"SarvamTTS error: {exc}")
        finally:
            yield TTSStoppedFrame()
