"""
ami_client.py — Minimal asyncio Asterisk Manager Interface (AMI) client.

Connects to Asterisk AMI on localhost:5038, logs in, and sends an Originate
action to dial out via the Alpha PBX SIP trunk.

Outbound call flow:
  POST /call/outbound  →  ami_client.originate()
    →  Asterisk AMI (port 5038)
      →  Asterisk dials SIP/alphapbx-trunk/<number>
        →  Alpha PBX routes the call to the destination
          →  When answered, Asterisk runs [outbound-bot] dialplan
            →  AudioSocket(127.0.0.1:9092)
              →  bridge_to_ws.py  →  Pipecat /ws  →  AI bot
"""

import asyncio
import os
from loguru import logger


class AsteriskAMIError(Exception):
    pass


class AsteriskAMIClient:
    """
    Thin asyncio AMI client — only what we need: login + Originate.
    AMI uses a line-based text protocol:
      key: value\r\n  (headers)
      \r\n            (blank line ends each packet)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5038,
        username: str = "pipecat",
        secret: str = "pipecat_secret",
        timeout: float = 10.0,
    ):
        self._host     = host
        self._port     = port
        self._username = username
        self._secret   = secret
        self._timeout  = timeout
        self._reader: asyncio.StreamReader | None  = None
        self._writer: asyncio.StreamWriter | None  = None

    # ── Connection ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=self._timeout,
        )
        # Consume the AMI banner line
        banner = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
        logger.debug(f"AMI banner: {banner.decode().strip()}")

        await self._login()

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    # ── Low-level send / receive ────────────────────────────────────────────────

    def _build_packet(self, fields: dict[str, str]) -> str:
        lines = "\r\n".join(f"{k}: {v}" for k, v in fields.items())
        return lines + "\r\n\r\n"

    async def _send(self, fields: dict[str, str]) -> None:
        pkt = self._build_packet(fields)
        self._writer.write(pkt.encode())
        await self._writer.drain()

    async def _recv_response(self) -> dict[str, str]:
        """Read AMI response packet (terminated by a blank line)."""
        result: dict[str, str] = {}
        while True:
            raw = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
            line = raw.decode(errors="replace").rstrip("\r\n")
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result

    # ── Auth ────────────────────────────────────────────────────────────────────

    async def _login(self) -> None:
        await self._send({
            "Action":   "Login",
            "Username": self._username,
            "Secret":   self._secret,
        })
        resp = await self._recv_response()
        if resp.get("Response") != "Success":
            raise AsteriskAMIError(f"AMI login failed: {resp.get('Message', resp)}")
        logger.info("AMI login successful")

    # ── Originate ───────────────────────────────────────────────────────────────

    async def originate(
        self,
        to_number: str,
        from_number: str,
        context: str      = "outbound-bot",
        exten: str        = "s",
        priority: int     = 1,
        caller_id: str    = "",
        timeout_ms: int   = 30000,
        variables: dict   | None = None,
    ) -> dict[str, str]:
        """
        Originate an outbound call through the Alpha PBX SIP trunk.

        Asterisk channel: SIP/alphapbx-trunk/<to_number>
        When the callee answers, Asterisk drops into [outbound-bot] dialplan
        which AudioSocket-bridges the audio to bridge_to_ws.py.
        """
        if not caller_id:
            caller_id = f"{from_number} <{from_number}>"

        # Build the Variable header (key=value pairs separated by |)
        var_parts: list[str] = [
            f"OUTBOUND_TO={to_number}",
            f"OUTBOUND_FROM={from_number}",
        ]
        if variables:
            var_parts += [f"{k}={v}" for k, v in variables.items()]

        fields: dict[str, str] = {
            "Action":    "Originate",
            "Channel":   f"SIP/alphapbx-trunk/{to_number}",
            "Context":   context,
            "Exten":     exten,
            "Priority":  str(priority),
            "Callerid":  caller_id,
            "Timeout":   str(timeout_ms),
            "Variable":  "|".join(var_parts),
            "Async":     "true",
        }

        logger.info(f"AMI Originate → {to_number}  (from {from_number})")
        await self._send(fields)
        resp = await self._recv_response()

        if resp.get("Response") not in ("Success", "Queued"):
            raise AsteriskAMIError(f"Originate failed: {resp.get('Message', resp)}")

        logger.info(f"AMI Originate accepted: {resp}")
        return resp


# ── Convenience helper ─────────────────────────────────────────────────────────

async def originate_call(
    to_number: str,
    from_number: str,
    variables: dict | None = None,
) -> dict[str, str]:
    """
    Open an AMI connection, originate one call, then close.
    Reads AMI credentials from environment variables.
    """
    client = AsteriskAMIClient(
        host     = os.getenv("AMI_HOST",     "127.0.0.1"),
        port     = int(os.getenv("AMI_PORT", "5038")),
        username = os.getenv("AMI_USERNAME", "pipecat"),
        secret   = os.getenv("AMI_SECRET",   "pipecat_secret"),
    )
    try:
        await client.connect()
        return await client.originate(
            to_number   = to_number,
            from_number = from_number,
            variables   = variables,
        )
    finally:
        await client.close()
