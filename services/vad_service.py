"""
Dynamic VAD threshold management.

Single VAD thresholds fail on:
- Noisy phone calls (echo, background chatter)
- Short single-word caller responses
- Agent turn (TTS playing) — TTS echo triggers false VAD events

Solution: swap named profiles at conversation phase transitions.
"""

from loguru import logger


# Thresholds tuned for 8 kHz telephony (empirical from Skoda project)
_PROFILES: dict[str, dict] = {
    "normal": {
        # Standard listening: tolerates brief pauses
        "start_secs": 0.1,
        "stop_secs": 0.7,
    },
    "attentive": {
        # Short answers (yes/no, single words) — catch quick bursts
        "start_secs": 0.04,
        "stop_secs": 0.5,
    },
    "agent_turn": {
        # Bot is speaking — suppress TTS echo from triggering STT
        "start_secs": 0.25,
        "stop_secs": 0.7,
    },
}


class VADService:
    """
    Wraps a Pipecat VAD analyzer and applies named threshold profiles.

    Usage:
        vad_svc = VADService(SileroVADAnalyzer())
        await vad_svc.set_profile("agent_turn")   # before TTS
        await vad_svc.set_profile("normal")        # after TTS finishes
    """

    def __init__(self, vad_analyzer):
        self._vad = vad_analyzer
        self._current = "normal"

    async def set_profile(self, profile: str) -> None:
        if profile == self._current:
            return
        params = _PROFILES.get(profile)
        if params is None:
            logger.warning(f"[VAD] unknown profile={profile!r} — keeping {self._current!r}")
            return

        self._current = profile
        logger.debug(f"[VAD] → {profile}: {params}")

        try:
            # SileroVADAnalyzer exposes start_secs / stop_secs as constructor
            # params, not runtime setters. Update them directly if attributes exist.
            for attr, val in params.items():
                if hasattr(self._vad, f"_{attr}") or hasattr(self._vad, attr):
                    target = f"_{attr}" if hasattr(self._vad, f"_{attr}") else attr
                    setattr(self._vad, target, val)
        except Exception as exc:
            logger.warning(f"[VAD] set_profile failed: {exc}")

    @property
    def current_profile(self) -> str:
        return self._current
