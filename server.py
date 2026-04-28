"""
server.py — FastAPI WebSocket server
Receives WebSocket connections from the SIP-WebSocket bridge and spawns
a Pipecat bot for every call.

Alpha PBX account (connektstudio):
  IP Number : 09647664295
  SIP Server: connektstudio.alphapbx.net:8090 (UDP)  /  :8091 (TLS)
  Extensions: 101 – 105  (AI bots can be assigned any extension)

SIP ↔ WebSocket bridge strategy
────────────────────────────────
  Alpha PBX routes calls to extension 101 (the bot).
  The SIP client on your server (Asterisk / FreeSWITCH / PJSUA) registers
  extension 101 against connektstudio.alphapbx.net:8090, then bridges
  incoming audio to this FastAPI /ws endpoint as raw PCM.

  Simplest local test:
    pjsua --id sip:101@connektstudio.alphapbx.net \\
          --proxy sip:connektstudio.alphapbx.net:8090 \\
          --password TFbY47W4tP8Jg4 \\
          --capture-dev -1 --playback-dev -1  # null audio — bot handles it

For local development, expose port 7860 with:
  ngrok http 7860
"""

import asyncio
import json
import os
import sys

import aiohttp
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from dotenv import load_dotenv

from bot import run_bot
from sip_outbound import make_outbound_call

load_dotenv(override=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="DEBUG")

# ─── Alpha PBX constants (pre-filled from welcome email) ──────────────────────
PBX_SIP_SERVER = os.getenv("PBX_SIP_SERVER")
PBX_SIP_PORT   = os.getenv("PBX_SIP_PORT_UDP")
PBX_EXTENSION  = os.getenv("PBX_EXTENSION")
PBX_EXT_PASS   = os.getenv("PBX_EXTENSION_PASSWORD")
PBX_DID        = os.getenv("PBX_DID_NUMBER")
PBX_IP_NUMBER  = os.getenv("PBX_IP_NUMBER")

# ─── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Alpha PBX · Pipecat Calling Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "pipecat-calling-agent",
        "sip_server": PBX_SIP_SERVER,
        "extension": PBX_EXTENSION,
        "did": PBX_IP_NUMBER,
    }


# ─── Dial-out trigger  (POST /call/outbound) ───────────────────────────────────
@app.post("/call/outbound")
async def trigger_outbound_call(request: Request):
    """
    Trigger an AI-driven outbound call via Asterisk AMI → Alpha PBX SIP trunk.

    Request body (JSON):
      {
        "to":   "01XXXXXXXXX",          # destination number (Bangladesh format OK)
        "from": "09647664295",          # caller-ID — defaults to your Alpha PBX DID
        "vars": { "key": "value" }      # optional — passed to the dialplan / bot
      }

    On success Asterisk dials the number through Alpha PBX.  When the callee
    answers, the audio is AudioSocket-bridged to bridge_to_ws.py → Pipecat /ws
    and the AI bot starts talking.

    Requires Asterisk running with:
      /etc/asterisk/manager.conf  — AMI credentials matching AMI_USERNAME/AMI_SECRET
      /etc/asterisk/sip.conf      — alphapbx-trunk peer registered
      /etc/asterisk/extensions.conf — [outbound-bot] context present
    """
    body = await request.json()
    to_number   = body.get("to", "").strip()
    from_number = body.get("from", PBX_DID or PBX_IP_NUMBER or "").strip()
    extra_vars  = body.get("vars", {})

    if not to_number:
        return JSONResponse(status_code=400, content={"error": "Missing 'to' field"})
    if not from_number:
        return JSONResponse(status_code=400, content={"error": "Missing 'from' field — set PBX_DID_NUMBER in .env"})

    logger.info(f"Outbound call requested: {from_number} → {to_number}")

    try:
        result = await make_outbound_call(
            to_number   = to_number,
            from_number = from_number,
        )
        return JSONResponse(status_code=200, content=result)

    except Exception as exc:
        logger.exception(f"Outbound call error: {exc}")
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ─── Webhook for inbound SIP/WebSocket stream ──────────────────────────────────
@app.api_route("/webhook/call", methods=["GET", "POST"])
async def call_webhook(request: Request):
    """
    Webhook pbx.bd (or your Asterisk/FreeSWITCH bridge) can call when a new
    inbound call arrives.  This returns XML/text instructions to redirect the
    audio stream to the /ws WebSocket endpoint.

    If your bridge speaks Twilio-compatible TwiML, return:
      <Response><Connect><Stream url="wss://YOUR_HOST/ws" /></Connect></Response>

    Otherwise implement bridge-specific instructions here.
    """
    host = request.headers.get("host", "your-server.example.com")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws" />
  </Connect>
</Response>"""
    return PlainTextResponse(content=twiml, media_type="text/xml")


# ─── WebSocket endpoint (main audio stream) ────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    pbx.bd's SIP bridge connects here.  We read the initial setup message
    to extract stream/call IDs, then hand off to the Pipecat bot.
    """
    await websocket.accept()
    logger.info("New WebSocket connection accepted")

    call_data: dict = {}

    try:
        # First frame from Twilio-compatible bridges is a JSON "connected" or
        # "start" message containing stream_sid, call_sid, and custom params.
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        msg = json.loads(raw)
        event = msg.get("event", "")

        if event == "connected":
            # Twilio sends "connected" first, then "start"
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            msg = json.loads(raw)
            event = msg.get("event", "")

        if event == "start":
            start = msg.get("start", {})
            call_data = {
                "stream_sid": msg.get("streamSid", ""),
                "call_sid":   start.get("callSid", ""),
                "from":       start.get("customParameters", {}).get("from", ""),
                **start.get("customParameters", {}),
            }
            logger.info(f"Call started: {call_data}")
        else:
            # If the bridge sends something else, still proceed with empty call_data
            logger.warning(f"Unexpected first frame event='{event}'. Proceeding anyway.")

    except asyncio.TimeoutError:
        logger.warning("No initial setup frame received within 10s; using empty call_data")
    except Exception as e:
        logger.error(f"Error reading initial frame: {e}")

    # ── Start the Pipecat bot ─────────────────────────────────────────────────
    try:
        await run_bot(websocket, call_data)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected cleanly")
    except Exception as e:
        logger.exception(f"Bot error: {e}")


# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 7860)),
        reload=False,
        log_level="info",
    )
