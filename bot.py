
import asyncio
import os
import sys
import argparse
import json
from datetime import datetime

from loguru import logger
from dotenv import load_dotenv

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.deepgram.stt import DeepgramSTTService
from sarvam_tts import SarvamTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from alpha_pbx_serializer import AlphaPBXSerializer

from services.call_data import CallData
from services.event_logger import fire_event
from services.idle_handler import IdleHandler
from services.vad_service import VADService
from services.tts_gate import TTSGateProcessor
from services import db, slack_service

load_dotenv(override=True)

# ─── System Prompt ─────────────────────────────────────────────────────────────
_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.md")
_BASE_PROMPT = open(_PROMPT_FILE, encoding="utf-8").read()


def _build_system_prompt() -> str:
    """Append live date/time so the bot always knows today's date."""
    now = datetime.now()
    date_ctx = (
        f"\n\n---\nআজকের তারিখ: {now.strftime('%Y-%m-%d')} "
        f"| সময়: {now.strftime('%H:%M')}"
    )
    return _BASE_PROMPT + date_ctx


async def run_bot(websocket, call_data: dict | None = None):
    """
    Entry-point called by server.py for every incoming WebSocket connection.
    """

    # ══════════════════════════════════════════════════════════════════
    # STEP 1 — Extract call metadata into passive CallData object
    # ══════════════════════════════════════════════════════════════════
    raw = call_data or {}
    call = CallData(
        call_sid=raw.get("call_sid", "unknown"),
        stream_sid=raw.get("stream_sid", "unknown"),
        caller_number=raw.get("from", "unknown"),
        called_number=raw.get("to", "unknown"),
        call_type=raw.get("call_type", "inbound"),
    )

    logger.info("━" * 60)
    logger.info("📞  New call received")
    logger.info(f"    call_sid     : {call.call_sid}")
    logger.info(f"    stream_sid   : {call.stream_sid}")
    logger.info(f"    caller number: {call.caller_number}")
    logger.info(f"    called number: {call.called_number}")
    logger.info(f"    call type    : {call.call_type}")
    logger.info(f"    SIP server   : {os.getenv('PBX_SIP_SERVER', 'connektstudio.alphapbx.net')}")
    logger.info(f"    extension    : {os.getenv('PBX_EXTENSION', '101')}")
    logger.info(f"    DID          : {os.getenv('PBX_DID_NUMBER', '09647664295')}")
    logger.info("━" * 60)

    fire_event(call.call_sid, "call_received", {
        "caller": call.caller_number,
        "called": call.called_number,
        "type": call.call_type,
    })

    # ══════════════════════════════════════════════════════════════════
    # STEP 2 — Audio serializer (μ-law ↔ PCM codec for Alpha PBX)
    # ══════════════════════════════════════════════════════════════════
    serializer = AlphaPBXSerializer(sample_rate=8000, num_channels=1)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3 — WebSocket transport + VAD service
    # ══════════════════════════════════════════════════════════════════
    vad_analyzer = SileroVADAnalyzer()
    vad_svc = VADService(vad_analyzer)

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=vad_analyzer,
            vad_audio_passthrough=True,
            serializer=serializer,
        ),
    )

    # ══════════════════════════════════════════════════════════════════
    # STEP 4 — AI services
    # ══════════════════════════════════════════════════════════════════
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        model="nova-3",
        language="multi",
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o",
    )

    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        speaker="pooja",
        language="bn-IN",
        model="bulbul:v3",
        pace=1.2,
        sample_rate=8000,
    )

    # ══════════════════════════════════════════════════════════════════
    # STEP 6 — TTS gate (blocks audio input while AI speaks)
    # ══════════════════════════════════════════════════════════════════
    tts_gate = TTSGateProcessor(transport)

    # ══════════════════════════════════════════════════════════════════
    # STEP 7 — Conversation context with live date/time injection
    # ══════════════════════════════════════════════════════════════════
    messages = [{"role": "system", "content": _build_system_prompt()}]

    if call.call_type == "outbound":
        messages.append({
            "role": "system",
            "content": (
                f"আপনি {call.called_number} নম্বরে কল করেছেন। "
                "দ্রুত এবং সংক্ষিপ্ত পরিচয় দিয়ে শুরু করুন — 'ওয়ালাইকুম আসসালাম! Connekt Studio থেকে বলছি।'"
            ),
        })
    elif call.caller_number and call.caller_number != "unknown":
        messages.append({
            "role": "system",
            "content": f"কলারের ফোন নম্বর: {call.caller_number}।",
        })

    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    # ══════════════════════════════════════════════════════════════════
    # STEP 8 — Pipeline
    # ══════════════════════════════════════════════════════════════════
    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        tts_gate,
        transport.output(),
        context_aggregator.assistant(),
    ])

    # ══════════════════════════════════════════════════════════════════
    # STEP 9 — Pipeline task
    # ══════════════════════════════════════════════════════════════════
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_logging=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    idle_handler = IdleHandler(task)

    # ══════════════════════════════════════════════════════════════════
    # STEP 10 — Event handlers
    # ══════════════════════════════════════════════════════════════════

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, websocket):
        logger.info("━" * 60)
        logger.info("📲  Client connected — sending greeting")
        logger.info("━" * 60)

        fire_event(call.call_sid, "client_connected", {"caller": call.caller_number})
        await slack_service.post_call_started(call.call_sid, call.caller_number, call.call_type)

        from pipecat.frames.frames import TTSSpeakFrame
        # Instant TTS greeting bypasses LLM latency — caller hears audio in ~500ms
        greeting = (
            "ওয়ালাইকুম আসসালাম! Connekt Studio থেকে বলছি। "
            "বলুন, কিভাবে সাহায্য করতে পারি?"
        )
        await task.queue_frames([
            TTSSpeakFrame(text=greeting),
            context_aggregator.user().get_context_frame(),
        ])
        # Start silence detection after greeting is queued
        idle_handler.arm()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, websocket):
        logger.info("━" * 60)
        logger.info("📴  Client disconnected — call ended")
        logger.info("━" * 60)

        idle_handler.reset()

        # Capture transcript from LLM context messages
        for msg in context.messages:
            role = msg.get("role", "")
            if role in ("user", "assistant"):
                text = msg.get("content", "")
                if isinstance(text, str) and text.strip():
                    call.add_transcript(role, text)

        call.mark_ended("completed")

        fire_event(call.call_sid, "call_ended", {
            "status": call.status,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
            "error_counters": call.error_counters,
        })

        # Fire-and-forget persistence — never blocks the disconnect handler
        await db.save_call(call.to_dict())
        await slack_service.post_call_ended(
            call.call_sid,
            call.status,
            [f"{t.role}: {t.text}" for t in call.transcripts],
            call.error_counters,
        )

        await task.cancel()

    # ══════════════════════════════════════════════════════════════════
    # STEP 11 — Run
    # ══════════════════════════════════════════════════════════════════
    logger.info("🚀  Pipeline running — waiting for audio")
    logger.info("━" * 60)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    logger.info(f"✅  Call {call.call_sid} finished cleanly")


# ─── CLI entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat bot (standalone test)")
    parser.add_argument("--call-data", type=str, default="{}", help="JSON call data")
    args = parser.parse_args()
    call_data = json.loads(args.call_data)
    logger.warning("Standalone mode: run via `uvicorn server:app` for real calls.")
