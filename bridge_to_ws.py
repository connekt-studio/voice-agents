#!/usr/bin/env python3
"""
bridge_to_ws.py — Asterisk AudioSocket ↔ Pipecat WebSocket bridge

How it works:
  Asterisk calls this bridge via its AudioSocket protocol (TCP).
  For every inbound SIP call from Alpha PBX the bridge:
    1. Accepts the Asterisk AudioSocket TCP connection
    2. Extracts caller-ID and unique call ID
    3. Connects to the Pipecat FastAPI /ws WebSocket
    4. Sends Twilio-compatible "connected" + "start" handshake
    5. Streams audio both ways in real-time:
         Asterisk  →  raw 8kHz SLIN PCM  →  μ-law base64 JSON  →  Pipecat /ws
         Pipecat /ws  →  μ-law base64 JSON  →  raw 8kHz SLIN PCM  →  Asterisk

Asterisk dialplan (extensions.conf):
  exten => 101,1,Answer()
  exten => 101,n,Set(CALLERID_VAR=${CALLERID(num)})
  exten => 101,n,AudioSocket(127.0.0.1:9092,${UNIQUEID})
  exten => 101,n,Hangup()

AudioSocket frame format (Asterisk → bridge):
  byte 0:     kind  (0x00=hangup, 0x01=SLIN audio, 0x02=UUID, 0xff=error)
  bytes 1-2:  payload length (big-endian uint16)
  bytes 3+:   payload

Run:
  python bridge_to_ws.py          # listens on 0.0.0.0:9092
"""

import asyncio
import audioop
import base64
import json
import os
import struct
import sys
import uuid

from loguru import logger
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
TCP_HOST  = os.getenv("BRIDGE_HOST", "0.0.0.0")
TCP_PORT  = int(os.getenv("BRIDGE_PORT", 9092))
WS_URL    = os.getenv("WS_URL", "ws://localhost:7860/ws")

# AudioSocket frame kinds
KIND_HANGUP = 0x00
KIND_AUDIO  = 0x01
KIND_UUID   = 0x02
KIND_ERROR  = 0xFF

FRAME_HEADER = 3          # kind(1) + length(2)
PCM_CHUNK    = 320        # 20ms × 8000Hz × 2 bytes = 320 bytes of PCM
MULAW_CHUNK  = 160        # 160 bytes μ-law per 20ms frame


# ── AudioSocket helpers ─────────────────────────────────────────────────────────

def make_frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">BH", kind, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(FRAME_HEADER)
    kind, length = struct.unpack(">BH", header)
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


async def write_frame(writer: asyncio.StreamWriter, kind: int, payload: bytes):
    writer.write(make_frame(kind, payload))
    await writer.drain()


# ── Per-call bridge ─────────────────────────────────────────────────────────────

async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    logger.info(f"AudioSocket connection from {peer}")

    call_uuid    = str(uuid.uuid4())
    caller_id    = "unknown"
    outbound_to  = ""          # set for AMI-originated (outbound) calls
    call_type    = "inbound"
    stream_sid   = f"asterisk-{call_uuid}"
    call_sid     = f"call-{call_uuid}"

    # ── Read UUID frame Asterisk sends first ────────────────────────────────────
    try:
        kind, payload = await asyncio.wait_for(read_frame(reader), timeout=5.0)
        if kind == KIND_UUID:
            call_uuid  = payload.decode(errors="replace").strip("\x00")
            stream_sid = f"asterisk-{call_uuid}"
            call_sid   = f"call-{call_uuid}"
            logger.info(f"Call UUID: {call_uuid}")
    except asyncio.TimeoutError:
        logger.warning("No UUID frame received; using generated UUID")

    # ── Detect outbound calls via Asterisk channel variables ────────────────────
    # Asterisk sets OUTBOUND_TO / OUTBOUND_FROM via AMI Variable field.
    # We cannot read channel vars over AudioSocket directly, but they are
    # embedded in the UUID string for outbound calls when set by the dialplan.
    # As a practical approach: the API caller can pass metadata via the AMI
    # Variable header; bridge reads CHANNEL env vars set on the TCP connection
    # if available, otherwise leaves defaults.
    # The bot context is enriched via customParameters in the WS handshake below.

    # ── Connect to Pipecat WebSocket ────────────────────────────────────────────
    try:
        import websockets  # noqa: PLC0415
        ws_conn = await websockets.connect(WS_URL)
    except Exception as exc:
        logger.error(f"Cannot connect to Pipecat WebSocket {WS_URL}: {exc}")
        writer.close()
        return

    logger.info(f"Connected to Pipecat WS: {WS_URL}")

    # ── Twilio-compatible handshake ─────────────────────────────────────────────
    await ws_conn.send(json.dumps({
        "event":    "connected",
        "protocol": "Call",
        "version":  "1.0.0",
    }))
    await ws_conn.send(json.dumps({
        "event":     "start",
        "streamSid": stream_sid,
        "start": {
            "callSid":    call_sid,
            "streamSid":  stream_sid,
            "accountSid": "alphapbx",
            "tracks":     ["inbound"],
            "mediaFormat": {
                "encoding":   "audio/x-mulaw",
                "sampleRate": 8000,
                "channels":   1,
            },
            "customParameters": {
                "from":      caller_id,
                "to":        outbound_to,
                "call_type": call_type,   # "inbound" | "outbound"
            },
        },
    }))

    seq = 1

    # ── Asterisk → WebSocket (inbound audio) ────────────────────────────────────
    async def asterisk_to_ws():
        nonlocal seq
        try:
            while True:
                kind, payload = await read_frame(reader)

                if kind == KIND_HANGUP or kind == KIND_ERROR:
                    logger.info(f"Asterisk sent kind=0x{kind:02x} — ending bridge")
                    break

                if kind == KIND_AUDIO and payload:
                    # PCM 16-bit signed → μ-law
                    mulaw = audioop.lin2ulaw(payload, 2)
                    await ws_conn.send(json.dumps({
                        "event":     "media",
                        "streamSid": stream_sid,
                        "media": {
                            "track":   "inbound",
                            "chunk":   str(seq),
                            "payload": base64.b64encode(mulaw).decode(),
                        },
                    }))
                    seq += 1

        except asyncio.IncompleteReadError:
            logger.info("Asterisk closed the connection")
        except Exception as exc:
            logger.exception(f"asterisk_to_ws error: {exc}")
        finally:
            await ws_conn.close()

    # ── WebSocket → Asterisk (bot audio out) ────────────────────────────────────
    async def ws_to_asterisk():
        try:
            async for raw in ws_conn:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event = msg.get("event", "")

                if event == "media":
                    payload_b64 = msg.get("media", {}).get("payload", "")
                    if payload_b64:
                        mulaw = base64.b64decode(payload_b64)
                        # μ-law → PCM 16-bit signed
                        pcm = audioop.ulaw2lin(mulaw, 2)
                        await write_frame(writer, KIND_AUDIO, pcm)

                elif event in ("stop", "disconnect"):
                    logger.info("Pipecat sent stop — ending bridge")
                    break

        except Exception as exc:
            logger.exception(f"ws_to_asterisk error: {exc}")
        finally:
            # Signal hangup to Asterisk
            try:
                await write_frame(writer, KIND_HANGUP, b"")
            except Exception:
                pass
            writer.close()

    # ── Run both directions concurrently ────────────────────────────────────────
    await asyncio.gather(asterisk_to_ws(), ws_to_asterisk())

    # ── Send stop event to Pipecat ───────────────────────────────────────────────
    try:
        await ws_conn.send(json.dumps({
            "event":     "stop",
            "streamSid": stream_sid,
            "stop":      {"callSid": call_sid},
        }))
    except Exception:
        pass

    logger.info(f"Call {call_uuid} bridging complete")


# ── TCP server ──────────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle_call, TCP_HOST, TCP_PORT)
    addrs = [str(s.getsockname()) for s in server.sockets]
    logger.info(f"AudioSocket bridge listening on {addrs}  →  Pipecat {WS_URL}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")
    asyncio.run(main())
