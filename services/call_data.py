"""
Passive per-call state container.

All state updates are synchronous in-memory.
Persistence (DB, Slack) is fire-and-forget async — never blocks the pipeline.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class TranscriptEntry:
    role: str       # "user" | "assistant"
    text: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class CallData:
    call_sid: str = "unknown"
    stream_sid: str = "unknown"
    caller_number: str = "unknown"
    called_number: str = "unknown"
    call_type: str = "inbound"

    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    ended_at: Optional[str] = None

    # "in-progress" | "completed" | "failed" | "escalated"
    status: str = "in-progress"

    transcripts: list = field(default_factory=list)

    # Per-failure-type counters — escalate at threshold
    error_counters: dict = field(default_factory=lambda: {
        "tts_failures": 0,
        "llm_failures": 0,
        "stt_failures": 0,
        "api_failures": 0,
    })

    recording_url: Optional[str] = None
    summary: Optional[str] = None

    def add_transcript(self, role: str, text: str) -> None:
        self.transcripts.append(TranscriptEntry(role=role, text=text))

    def increment_error(self, error_type: str) -> int:
        """Increment and return the new count for the given error type."""
        count = self.error_counters.get(error_type, 0) + 1
        self.error_counters[error_type] = count
        return count

    def mark_ended(self, status: str = "completed") -> None:
        self.status = status
        self.ended_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "call_sid": self.call_sid,
            "stream_sid": self.stream_sid,
            "caller_number": self.caller_number,
            "called_number": self.called_number,
            "call_type": self.call_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "transcripts": [
                {"role": t.role, "text": t.text, "timestamp": t.timestamp}
                for t in self.transcripts
            ],
            "error_counters": self.error_counters,
            "recording_url": self.recording_url,
            "summary": self.summary,
        }
