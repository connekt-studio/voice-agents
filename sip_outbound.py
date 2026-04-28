"""
sip_outbound.py — Direct SIP outbound calling via pyVoIP (no Asterisk needed).

Registers extension 101 directly with Alpha PBX and dials out.
When the callee answers, RTP audio is bridged to the Pipecat /ws endpoint
via a local WebSocket connection — reusing the existing bot pipeline.

Architecture:
  POST /call/outbound
    → make_outbound_call()
      → pyVoIP dials via SIP to connektstudio.alphapbx.net:8090
        → Alpha PBX routes the call to the destination number
          → Callee answers → RTP audio stream established
            → sip_to_ws()  : RTP PCM → μ-law → WebSocket → Pipecat bot
            → ws_to_sip()  : Pipecat bot → μ-law → PCM → RTP

ROOT CAUSE of "TODO: Add 500 Error" spam (FIXED HERE):
  Alpha PBX (FreeSWITCH) returns SIP 407 Proxy Authentication Required
  on every outbound INVITE.  pyVoIP 1.6.8 only handles 401 WWW-Auth in
  its invite() method — the 407 proxy-auth path is stubbed as TODO.
  We monkey-patch SIPClient.invite() below to handle 407 properly by
  retrying the INVITE with a Proxy-Authorization: Digest header.
"""

import asyncio
import audioop
import base64
import json
import logging
import os
import threading
import time
import uuid

import websockets
from loguru import logger
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Enable pyVoIP verbose logging when PYVOIP_DEBUG=1 ────────────────────────
# pyVoIP uses its OWN global (pyVoIP.DEBUG), NOT Python's logging module.
# We also monkey-patch pyVoIP.debug() so SIP messages flow through loguru.
if os.getenv("PYVOIP_DEBUG", "0") == "1":
    import pyVoIP as _pyvoip_pkg
    _pyvoip_pkg.DEBUG = True          # ← this is what actually enables full output

    # Redirect pyVoIP's print()-based debug to loguru so it appears in our log
    def _pyvoip_debug_to_loguru(s, e=None):
        if _pyvoip_pkg.DEBUG:
            logger.debug(f"[pyVoIP] {s}")
        elif e is not None:
            logger.warning(f"[pyVoIP] {e}")

    _pyvoip_pkg.debug = _pyvoip_debug_to_loguru
    # Also patch the reference already imported in pyVoIP.SIP
    try:
        import pyVoIP.SIP as _sip_mod
        _sip_mod.debug = _pyvoip_debug_to_loguru
    except Exception:
        pass
    logger.info("pyVoIP DEBUG logging enabled (pyVoIP.DEBUG=True, output → loguru)")


# ── Monkey-patch pyVoIP to handle SIP 407 Proxy Authentication Required ──────
# Alpha PBX (FreeSWITCH) challenges every outbound INVITE with 407.
# pyVoIP 1.6.8's SIPClient.invite() only handles 401; 407 hits the
# "TODO: Add 500 Error" stub and the call stays stuck in DIALING forever.
# This patch adds 407 handling identical to 401 but using Proxy-Authorization.
def _patch_pyvoip_407():
    try:
        import hashlib
        import pyVoIP.SIP as _sip
        from pyVoIP.SIP import SIPMessage, SIPStatus, SIPClient

        _orig_invite = SIPClient.invite

        def _patched_invite(self, number, ms, sendtype):
            """
            Drop-in replacement for SIPClient.invite() that handles
            407 Proxy Authentication Required in addition to 401.
            """
            branch  = "z9hG4bK" + self.gen_call_id()[0:25]
            call_id = self.gen_call_id()
            sess_id = self.sessID.next()
            invite  = self.gen_invite(number, str(sess_id), ms, sendtype, branch, call_id)

            with self.recvLock:
                self.out.sendto(invite.encode("utf8"), (self.server, self.port))

                # Collect the first non-provisional, same call-ID response
                response = SIPMessage(self.s.recv(8192))
                while (
                    response.status not in (
                        SIPStatus(401), SIPStatus(407),
                        SIPStatus(100), SIPStatus(180),
                    )
                    or response.headers["Call-ID"] != call_id
                ):
                    if not self.NSD:
                        break
                    self.parse_message(response)
                    response = SIPMessage(self.s.recv(8192))

                # 100 Trying / 180 Ringing — call is in progress, return
                if response.status in (SIPStatus(100), SIPStatus(180)):
                    return SIPMessage(invite.encode("utf8")), call_id, sess_id

                # ── 401 WWW-Authenticate (original behaviour) ────────────
                if response.status == SIPStatus(401):
                    ack = self.gen_ack(response)
                    self.out.sendto(ack.encode("utf8"), (self.server, self.port))
                    authhash = self.gen_authorization(response)
                    nonce = response.authentication["nonce"]
                    realm = response.authentication["realm"]
                    auth = (
                        f'Authorization: Digest username="{self.username}",realm='
                        + f'"{realm}",nonce="{nonce}",uri="sip:{self.server};'
                        + f'transport=UDP",response="{str(authhash, "utf8")}",'
                        + "algorithm=MD5\r\n"
                    )
                    invite2 = self.gen_invite(number, str(sess_id), ms, sendtype, branch, call_id)
                    invite2 = invite2.replace("\r\nContent-Length", f"\r\n{auth}Content-Length")
                    self.out.sendto(invite2.encode("utf8"), (self.server, self.port))
                    return SIPMessage(invite2.encode("utf8")), call_id, sess_id

                # ── 407 Proxy-Authenticate (the fix) ────────────────────
                if response.status == SIPStatus(407):
                    logger.info("[pyVoIP-patch] Got 407 Proxy Auth — retrying INVITE with Proxy-Authorization")
                    ack = self.gen_ack(response)
                    self.out.sendto(ack.encode("utf8"), (self.server, self.port))

                    # pyVoIP only parses WWW-Authenticate into .authentication;
                    # Proxy-Authenticate comes back as a raw string from the else branch.
                    # Parse it manually with regex.
                    import re as _re
                    raw_proxy_auth = response.headers.get("Proxy-Authenticate", "")
                    if isinstance(raw_proxy_auth, str):
                        # Extract key="value" or key=value pairs from the Digest string
                        pairs = _re.findall(r'(\w+)=["\']?([^"\',\s]+)["\']?', raw_proxy_auth)
                        auth_hdr = {k: v for k, v in pairs}
                    elif isinstance(raw_proxy_auth, dict):
                        auth_hdr = raw_proxy_auth
                    else:
                        # fall back to whatever pyVoIP put in .authentication
                        auth_hdr = response.authentication or {}

                    logger.debug(f"[pyVoIP-patch] Parsed Proxy-Authenticate: {auth_hdr}")
                    realm = auth_hdr.get("realm", self.server)
                    nonce = auth_hdr.get("nonce", "")
                    qop   = auth_hdr.get("qop", "")
                    uri   = f"sip:{self.server}"

                    ha1 = hashlib.md5(f"{self.username}:{realm}:{self.password}".encode()).hexdigest()
                    ha2 = hashlib.md5(f"INVITE:{uri}".encode()).hexdigest()
                    if qop:
                        nc   = "00000001"
                        cnonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
                        resp_hash = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
                        proxy_auth = (
                            f'Proxy-Authorization: Digest username="{self.username}",'
                            f'realm="{realm}",nonce="{nonce}",uri="{uri}",'
                            f'response="{resp_hash}",algorithm=MD5,'
                            f'qop={qop},nc={nc},cnonce="{cnonce}"\r\n'
                        )
                    else:
                        resp_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                        proxy_auth = (
                            f'Proxy-Authorization: Digest username="{self.username}",'
                            f'realm="{realm}",nonce="{nonce}",uri="{uri}",'
                            f'response="{resp_hash}",algorithm=MD5\r\n'
                        )

                    invite2 = self.gen_invite(number, str(sess_id), ms, sendtype, branch, call_id)
                    invite2 = invite2.replace("\r\nContent-Length", f"\r\n{proxy_auth}Content-Length")
                    self.out.sendto(invite2.encode("utf8"), (self.server, self.port))
                    logger.info("[pyVoIP-patch] Re-INVITE with Proxy-Authorization sent")
                    return SIPMessage(invite2.encode("utf8")), call_id, sess_id

                # Any other response — fall through to original behaviour
                return _orig_invite(self, number, ms, sendtype)

        SIPClient.invite = _patched_invite
        logger.info("[pyVoIP-patch] SIPClient.invite() patched to handle 407 Proxy Auth")
    except Exception as exc:
        logger.warning(f"[pyVoIP-patch] Could not patch SIPClient.invite(): {exc}")

_patch_pyvoip_407()

# ── Alpha PBX SIP credentials ──────────────────────────────────────────────────
SIP_SERVER = os.getenv("PBX_SIP_SERVER", "connektstudio.alphapbx.net")
SIP_PORT   = int(os.getenv("PBX_SIP_PORT_UDP", "8090"))
SIP_EXT    = os.getenv("PBX_EXTENSION", "101")
SIP_PASS   = os.getenv("PBX_EXTENSION_PASSWORD", "TFbY47W4tP8Jg4")
WS_URL     = os.getenv("WS_URL", "ws://localhost:7860/ws")

# ── IP address helpers ────────────────────────────────────────────────────────────────
# pyVoIP.myIP   = local interface IP  — what the OS socket is bound to.
# SIP Via/Contact headers use myIP too, so Alpha PBX replies to that address.
# Behind NAT your router forwards external port 5060 → 192.168.x.x:5060.
#
# If you have a static public IP directly on the interface (e.g. a VPS/cloud
# server), set SIP_MY_IP to that public IP and it will be used directly.

def _get_local_ip() -> str:
    """Return the local interface IP that the OS can actually bind to."""
    override = os.getenv("SIP_MY_IP", "").strip()
    if override:
        return override
    try:
        import socket as _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect((SIP_SERVER, SIP_PORT))
        ip = s.getsockname()[0]
        s.close()
        logger.info(f"Auto-detected local interface IP: {ip}")
        return ip
    except Exception as exc:
        logger.warning(f"Could not detect local IP ({exc}), using 0.0.0.0")
        return "0.0.0.0"


# Singleton phone — one registration, many calls
_phone      = None
_phone_lock = threading.Lock()


def _get_phone():
    """Return a started VoIPPhone, registering with Alpha PBX once."""
    global _phone
    with _phone_lock:
        if _phone is not None:
            return _phone

        from pyVoIP.VoIP.VoIP import VoIPPhone

        local_ip = _get_local_ip()
        logger.info(
            f"Registering SIP {SIP_EXT}@{SIP_SERVER}:{SIP_PORT} …"
            f"  Binding to local IP: {local_ip}:5060"
        )
        phone = VoIPPhone(
            server   = SIP_SERVER,
            port     = SIP_PORT,
            username = SIP_EXT,
            password = SIP_PASS,
            myIP     = local_ip,   # ← local interface IP (bindable by the OS)
            sipPort  = 5060,
        )
        phone.start()
        # Give the REGISTER a moment to be acknowledged
        time.sleep(2.0)
        logger.info(f"SIP registration OK (bound to {local_ip}:5060)")
        _phone = phone
        return _phone


def _normalize_number(number: str) -> str:
    """
    Normalize a Bangladeshi number to E.164 format (+8801XXXXXXXXX).
    Alpha PBX requires the full international format in the SIP INVITE.
    """
    n = number.strip().replace(" ", "").replace("-", "")
    if n.startswith("+880"):          # already full E.164 e.g. +8801XXXXXXXXX
        return n
    if n.startswith("+88") and not n.startswith("+880"):
        # Likely already correct but double-check length
        return n
    if n.startswith("880") and len(n) == 13:   # 8801XXXXXXXXX → +8801XXXXXXXXX
        return f"+{n}"
    if n.startswith("88") and len(n) >= 12:     # 88... without 0 prefix
        return f"+{n}"
    if n.startswith("0") and len(n) == 11:      # 01XXXXXXXXX → +8801XXXXXXXXX
        return f"+880{n[1:]}"                   # 0→ drop, prepend +880
    if not n.startswith("+") and len(n) == 10:  # 1XXXXXXXXX → +8801XXXXXXXXX
        return f"+880{n}"
    return n                                    # already formatted or unknown


def _candidate_numbers(normalized: str) -> list:
    """
    Return all number formats to try when pyVoIP gets a SIP error.
    Alpha PBX may accept only one specific format in the INVITE URI.

    Order: E.164 (+8801…) → without-plus (8801…) → local (01…)
    """
    candidates = [normalized]  # already E.164 e.g. +8801XXXXXXXXX
    if normalized.startswith("+"):
        without_plus = normalized[1:]           # 8801XXXXXXXXX
        local = without_plus[2:] if without_plus.startswith("88") else without_plus  # 01XXXXXXXXX
        candidates += [without_plus, local]
    return candidates


def _dial_and_bridge_sync(phone, candidates: list, from_number: str, loop):
    """
    Blocking worker — runs in a plain thread (not the asyncio thread pool).
    Dials each candidate number format until one succeeds, then runs the
    async bridge coroutine on the provided event loop.

    This must be called via threading.Thread so that asyncio.CancelledError
    from the HTTP request lifecycle cannot propagate into phone.call().
    """
    from pyVoIP.VoIP.VoIP import CallState

    call        = None
    used_number = candidates[0]

    for fmt in candidates:
        logger.info(f"  [dial-thread] Trying number format: {fmt}")
        try:
            c = phone.call(fmt)
        except Exception as exc:
            logger.warning(f"  [dial-thread] phone.call({fmt!r}) raised: {exc}")
            continue

        # ── Phase 1: Wait up to 3 s for an immediate ENDED (hard failure) ─
        # phone.call() raises on auth/network errors.  If the call is alive
        # and DIALING after 3 s, Alpha PBX is ringing the callee — proceed.
        logger.info(f"  [dial-thread] Waiting for initial SIP response for {fmt!r} …")
        for _tick in range(30):    # 30 × 0.1 s = 3 s
            time.sleep(0.1)
            state = c.state
            if state in (CallState.ENDED, CallState.ANSWERED):
                break

        if state == CallState.ENDED:
            logger.warning(f"  [dial-thread] {fmt!r} → ENDED immediately. Trying next format.")
            try:
                c.hangup()
            except Exception:
                pass
            continue

        if state == CallState.ANSWERED:
            logger.info(f"  [dial-thread] {fmt!r} → ANSWERED immediately!")

        if state == CallState.DIALING:
            # 183 Session Progress already received (ringing) — hand off to bridge
            logger.info(f"  [dial-thread] {fmt!r} is DIALING/RINGING — passing to bridge (60 s answer window)")

        # Hand off to bridge regardless of DIALING or ANSWERED
        call        = c
        used_number = fmt
        logger.info(f"  [dial-thread] Using {used_number!r} (state={state})")
        break


    if call is None:
        logger.error(
            f"All number formats rejected by Alpha PBX. "
            "Likely causes: (1) wrong number format, (2) no PSTN outbound credit, "
            "(3) NAT — verify SIP_MY_IP in .env matches your public IP."
        )
        return

    # Hand off to the async bridge (schedule on the running event loop)
    future = asyncio.run_coroutine_threadsafe(
        _bridge_call(call, used_number, from_number, loop),
        loop,
    )
    try:
        future.result()   # wait so thread stays alive while the call is up
    except Exception as exc:
        logger.error(f"[dial-thread] Bridge error: {exc}")


async def make_outbound_call(to_number: str, from_number: str) -> dict:
    """
    Fire-and-forget outbound call.

    Returns {"status": "initiated"} immediately.  The SIP dial + audio
    bridge happen in a daemon thread so FastAPI request cancellation
    cannot interrupt pyVoIP's blocking phone.call().
    """
    loop = asyncio.get_event_loop()

    # ── 1. Register (or reuse) the SIP phone ──────────────────────────────────
    phone = await loop.run_in_executor(None, _get_phone)

    # ── 2. Normalize number and build candidate list ───────────────────────────
    normalized = _normalize_number(to_number)
    candidates = _candidate_numbers(normalized)
    logger.info(
        f"Dialing {normalized} (raw: {to_number}) via SIP …"
        f"  Will try formats in order: {candidates}"
    )

    # ── 3. Dial + bridge in an isolated daemon thread ─────────────────────────
    # Using a plain Thread (not the asyncio thread pool) ensures that when
    # FastAPI cancels this coroutine the cancellation signal does NOT reach
    # pyVoIP's blocking phone.call() call inside the thread.
    t = threading.Thread(
        target  = _dial_and_bridge_sync,
        args    = (phone, candidates, from_number, loop),
        daemon  = True,
        name    = f"sip-dial-{normalized}",
    )
    t.start()
    logger.info(f"Dial thread started: {t.name}")

    return {"status": "initiated", "to": normalized, "from": from_number}


async def _bridge_call(call, to_number: str, from_number: str, loop):
    """
    Wait for the callee to answer, then bridge audio between the SIP call
    and the Pipecat WebSocket bot.
    """
    from pyVoIP.VoIP.VoIP import CallState

    call_uuid  = str(uuid.uuid4())
    stream_sid = f"sip-{call_uuid}"
    call_sid   = f"call-{call_uuid}"

    # ── Wait for answer (60 s ring timeout) ──────────────────────────────────
    # 407 Proxy Auth is now handled — call stays in DIALING while ringing.
    # We wait up to 60 s for the callee to answer (SIP 200 OK → ANSWERED).
    logger.info(f"Waiting for {to_number} to answer … (60 s timeout, poll every 0.1 s)")
    dialing_ticks = 0
    prev_state    = None
    for tick in range(600):   # 600 × 0.1 s = 60 s
        state = call.state
        if state != prev_state:
            logger.info(f"  [tick {tick:03d}] SIP call state changed: {prev_state} → {state}")
            prev_state = state

        if state == CallState.ANSWERED:
            break
        if state == CallState.ENDED:
            logger.warning(
                f"Call to {to_number} ended before answer. "
                "This usually means Alpha PBX returned 4xx/5xx. "
                "Add PYVOIP_DEBUG=1 to .env and restart to see the SIP response code."
            )
            return
        if state == CallState.DIALING:
            dialing_ticks += 1
        else:
            dialing_ticks = 0

        await asyncio.sleep(0.1)
    else:
        logger.error(f"Call to {to_number} not answered within 60 s — hanging up")
        await loop.run_in_executor(None, call.hangup)
        return

    logger.info(f"Call answered! Bridging to Pipecat bot …")

    # ── Connect to Pipecat WebSocket ──────────────────────────────────────────
    try:
        ws = await websockets.connect(WS_URL)
    except Exception as exc:
        logger.error(f"Cannot connect to Pipecat WS {WS_URL}: {exc}")
        await loop.run_in_executor(None, call.hangup)
        return

    # ── Twilio-compatible handshake ───────────────────────────────────────────
    await ws.send(json.dumps({
        "event":    "connected",
        "protocol": "Call",
        "version":  "1.0.0",
    }))
    await ws.send(json.dumps({
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
                "from":      from_number,
                "to":        to_number,
                "call_type": "outbound",
            },
        },
    }))

    seq = 0

    # ── SIP RTP → WebSocket (caller audio → Pipecat STT) ─────────────────────
    async def sip_to_ws():
        nonlocal seq
        try:
            while call.state == CallState.ANSWERED:
                # readAudio returns 8-bit PCM (width=1) — pyVoIP decodes μ-law with audioop.ulaw2lin(data, 1)
                pcm8 = await loop.run_in_executor(None, call.readAudio, 160)
                if not pcm8:
                    await asyncio.sleep(0.01)
                    continue
                # Upscale 8-bit → 16-bit PCM for Pipecat's ulaw_to_pcm/STT pipeline
                pcm16 = audioop.lin2lin(pcm8, 1, 2)
                mulaw = audioop.lin2ulaw(pcm16, 2)  # encode as 16-bit μ-law for transport
                seq += 1
                await ws.send(json.dumps({
                    "event":     "media",
                    "streamSid": stream_sid,
                    "media": {
                        "track":   "inbound",
                        "chunk":   str(seq),
                        "payload": base64.b64encode(mulaw).decode(),
                    },
                }))
        except Exception as exc:
            logger.error(f"sip_to_ws: {exc}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    # ── WebSocket (Pipecat TTS) → SIP RTP ────────────────────────────────────
    # TTS audio arrives faster than real-time.  We pace writes to pyVoIP
    # at the RTP rate (20 ms per 160-sample 8-bit chunk) so the RTP sender
    # doesn't burst-send a full TTS response and cause jitter/skipping.
    CHUNK_SAMPLES  = 160                   # 20 ms at 8 kHz
    CHUNK_DURATION = CHUNK_SAMPLES / 8000  # 0.020 s

    async def ws_to_sip():
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                event = msg.get("event", "")
                if event == "media":
                    payload_b64 = msg.get("media", {}).get("payload", "")
                    if not payload_b64:
                        continue
                    mulaw16 = base64.b64decode(payload_b64)
                    pcm16   = audioop.ulaw2lin(mulaw16, 2)
                    pcm8    = audioop.lin2lin(pcm16, 2, 1)

                    # Write in 160-byte chunks, sleeping 20 ms between each
                    # so pyVoIP's RTP sender stays in sync with real time.
                    for i in range(0, len(pcm8), CHUNK_SAMPLES):
                        chunk = pcm8[i : i + CHUNK_SAMPLES]
                        await loop.run_in_executor(None, call.writeAudio, chunk)
                        await asyncio.sleep(CHUNK_DURATION)

                elif event in ("stop", "disconnect"):
                    logger.info("Pipecat sent stop — ending call")
                    break
        except Exception as exc:
            logger.error(f"ws_to_sip: {exc}")
        finally:
            if call.state == CallState.ANSWERED:
                await loop.run_in_executor(None, call.hangup)
            logger.info(f"Outbound call to {to_number} ended")


    await asyncio.gather(sip_to_ws(), ws_to_sip())
