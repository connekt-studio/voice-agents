"""
test_call.py — Simulates a phone call WebSocket connection to your Pipecat server.
Sends a fake "start" handshake, then streams a short μ-law audio clip so the
bot hears something and responds.

Usage:
    .venv/bin/python test_call.py
"""

import asyncio
import json
import base64
import audioop   # stdlib μ-law encoder
import wave
import os
import sys

try:
    import websockets
except ImportError:
    print("Run: .venv/bin/pip install websockets")
    sys.exit(1)

WS_URL      = "ws://localhost:7860/ws"
STREAM_SID  = "MX_test_stream_001"
CALL_SID    = "CA_test_call_001"

# ── Generate a simple 1-second sine-wave tone at 8kHz (μ-law) ─────────────────
def make_mulaw_tone(freq=440, duration_ms=1000, sample_rate=8000) -> bytes:
    import math
    num_samples = int(sample_rate * duration_ms / 1000)
    # 16-bit PCM sine wave
    pcm = bytearray()
    for i in range(num_samples):
        sample = int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
        pcm += sample.to_bytes(2, "little", signed=True)
    # Convert PCM → μ-law
    return audioop.lin2ulaw(bytes(pcm), 2)


async def simulate_call():
    print(f"🔌 Connecting to {WS_URL} ...")
    async with websockets.connect(WS_URL) as ws:
        print("✅ Connected!\n")

        # ── 1. Send "connected" event ──────────────────────────────────────────
        await ws.send(json.dumps({
            "event": "connected",
            "protocol": "Call",
            "version": "1.0.0"
        }))
        print("→ Sent: connected event")

        # ── 2. Send "start" event (call metadata) ─────────────────────────────
        await ws.send(json.dumps({
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": STREAM_SID,
            "start": {
                "callSid":    CALL_SID,
                "streamSid":  STREAM_SID,
                "accountSid": "TEST_ACCOUNT",
                "tracks":     ["inbound"],
                "mediaFormat": {
                    "encoding":   "audio/x-mulaw",
                    "sampleRate": 8000,
                    "channels":   1
                },
                "customParameters": {
                    "from": "+8801516701795"
                }
            }
        }))
        print("→ Sent: start event (call metadata)\n")
        print("⏳ Waiting for bot to process and respond …\n")

        # ── 3. Stream μ-law audio frames (simulate caller speaking) ────────────
        mulaw_audio = make_mulaw_tone(freq=440, duration_ms=2000)
        chunk_size  = 160   # 20ms worth at 8kHz = 160 bytes

        seq = 2
        for i in range(0, len(mulaw_audio), chunk_size):
            chunk = mulaw_audio[i:i + chunk_size]
            await ws.send(json.dumps({
                "event":          "media",
                "sequenceNumber": str(seq),
                "streamSid":      STREAM_SID,
                "media": {
                    "track":     "inbound",
                    "chunk":     str(seq),
                    "timestamp": str(seq * 20),
                    "payload":   base64.b64encode(chunk).decode()
                }
            }))
            seq += 1
            await asyncio.sleep(0.02)   # real-time pacing (20ms per frame)

        print("→ Finished streaming 2s of test audio\n")
        print("📥 Listening for bot responses (10 seconds) …\n")

        # ── 4. Listen for bot audio responses ─────────────────────────────────
        try:
            async with asyncio.timeout(10):
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "media":
                        payload = data.get("media", {}).get("payload", "")
                        audio   = base64.b64decode(payload)
                        print(f"🤖 Bot audio frame received: {len(audio)} bytes (μ-law)")

                    elif event == "mark":
                        print(f"📍 Mark: {data.get('mark', {}).get('name', '')}")

                    else:
                        print(f"📨 Event: {event}")

        except asyncio.TimeoutError:
            print("\n⏱  10-second listening window complete.")

        # ── 5. Send "stop" event ──────────────────────────────────────────────
        await ws.send(json.dumps({
            "event":          "stop",
            "sequenceNumber": str(seq),
            "streamSid":      STREAM_SID,
            "stop": {"callSid": CALL_SID}
        }))
        print("\n→ Sent: stop event (call ended)")
        print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(simulate_call())
