
import asyncio
import os
import sys
import argparse
import json

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

load_dotenv(override=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="DEBUG")


# ─── System Prompt ─────────────────────────────────────────────────────────────
_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.md")
SYSTEM_PROMPT = open(_PROMPT_FILE, encoding="utf-8").read()


async def run_bot(websocket, call_data: dict | None = None):
    """
    Entry-point called by server.py for every incoming WebSocket connection
    from pbx.bd (or any SIP/WebSocket bridge).
    """

    # ══════════════════════════════════════════════════════════════════
    # STEP 1 — Call received
    # ══════════════════════════════════════════════════════════════════
    stream_sid    = (call_data or {}).get("stream_sid", "unknown")
    call_sid      = (call_data or {}).get("call_sid",   "unknown")
    caller_number = (call_data or {}).get("from", "unknown")
    called_number = (call_data or {}).get("to",   "unknown")
    call_type     = (call_data or {}).get("call_type", "inbound")

    logger.info("━" * 60)
    logger.info("📞  STEP 1 — New call received")
    logger.info(f"    call_sid     : {call_sid}")
    logger.info(f"    stream_sid   : {stream_sid}")
    logger.info(f"    caller number: {caller_number}")
    logger.info(f"    called number: {called_number}")
    logger.info(f"    call type    : {call_type}")
    logger.info(f"    SIP server   : {os.getenv('PBX_SIP_SERVER', 'connektstudio.alphapbx.net')}")
    logger.info(f"    extension    : {os.getenv('PBX_EXTENSION', '101')}")
    logger.info(f"    DID          : {os.getenv('PBX_DID_NUMBER', '09647664295')}")
    logger.info("━" * 60)

    # ══════════════════════════════════════════════════════════════════
    # STEP 2 — Create audio serializer (μ-law ↔ PCM codec for Alpha PBX)
    # ══════════════════════════════════════════════════════════════════
    logger.info("🔧  STEP 2 — Creating AlphaPBXSerializer (raw μ-law for Alpha PBX SIP — no Twilio)")
    serializer = AlphaPBXSerializer(
        sample_rate=8000,
        num_channels=1,
    )
    logger.info("    ✅ Serializer ready (Alpha PBX SIP — no Twilio credentials needed)")

    # ══════════════════════════════════════════════════════════════════
    # STEP 3 — Set up WebSocket transport
    # ══════════════════════════════════════════════════════════════════
    logger.info("🔌  STEP 3 — Setting up FastAPI WebSocket transport")
    logger.info("    audio_in : enabled  (8 kHz μ-law from SIP bridge)")
    logger.info("    audio_out: enabled  (8 kHz μ-law to SIP bridge)")
    logger.info("    VAD      : Silero   (voice activity detection)")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            serializer=serializer,
        ),
    )
    logger.info("    ✅ Transport ready")

    # ══════════════════════════════════════════════════════════════════
    # STEP 4 — Initialize AI services
    # ══════════════════════════════════════════════════════════════════
    logger.info("🎤  STEP 4a — Initializing Deepgram STT (Speech → Text)")
    logger.info("    model   : nova-3")
    logger.info("    language: multi  (Bangla + English auto-detect)")
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        model="nova-3",
        language="multi",
    )
    logger.info("    ✅ STT ready")

    logger.info("🧠  STEP 4b — Initializing OpenAI LLM (Text → Text)")
    logger.info("    model: gpt-4o")
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o",
    )
    logger.info("    ✅ LLM ready")

    logger.info("🔊  STEP 4c — Initializing Sarvam TTS (Text → Bangla Speech)")
    logger.info("    model      : bulbul:v3")
    logger.info("    speaker    : pooja")
    logger.info("    language   : bn-IN")
    logger.info("    sample_rate: 8000 Hz")
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        speaker="pooja",
        language="bn-IN",
        model="bulbul:v3",
        pace=1.0,
        sample_rate=8000,
    )
    logger.info("    ✅ Sarvam TTS ready")

    # ══════════════════════════════════════════════════════════════════
    # STEP 5 — Build conversation context
    # ══════════════════════════════════════════════════════════════════
    logger.info("💬  STEP 5 — Building conversation context")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    logger.info("    ✅ System prompt loaded")

    if call_type == "outbound":
        messages.append({
            "role": "system",
            "content": (
                f"You initiated this call to {called_number}. "
                "Greet them first with a warm introduction explaining why you are calling."
            ),
        })
        logger.info(f"    ✅ Outbound context added: calling {called_number}")
    elif caller_number and caller_number != "unknown":
        messages.append({
            "role": "system",
            "content": f"The caller's phone number is {caller_number}.",
        })
        logger.info(f"    ✅ Caller info added: {caller_number}")
    else:
        logger.info("    ℹ️  No caller number — using generic greeting")

    context            = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)
    logger.info("    ✅ Context aggregator ready")

    # ══════════════════════════════════════════════════════════════════
    # STEP 6 — Assemble the pipeline
    # ══════════════════════════════════════════════════════════════════
    logger.info("⚙️   STEP 6 — Assembling pipeline")
    logger.info("    WebSocket audio in")
    logger.info("    → Silero VAD  (detects when user is speaking)")
    logger.info("    → Deepgram STT  (speech → text)")
    logger.info("    → LLM context user aggregator")
    logger.info("    → OpenAI GPT-4o  (generates reply)")
    logger.info("    → Sarvam TTS bulbul:v3  (text → Bangla speech)")
    logger.info("    → WebSocket audio out")
    logger.info("    → LLM context assistant aggregator")

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    logger.info("    ✅ Pipeline assembled (7 stages)")

    # ══════════════════════════════════════════════════════════════════
    # STEP 7 — Create pipeline task
    # ══════════════════════════════════════════════════════════════════
    logger.info("📋  STEP 7 — Creating PipelineTask")
    logger.info("    allow_interruptions : True  (caller can cut off bot)")
    logger.info("    enable_metrics      : True")
    logger.info("    audio sample rate   : 8000 Hz in/out")

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
    logger.info("    ✅ Task created")

    # ══════════════════════════════════════════════════════════════════
    # STEP 8 — Register event handlers
    # ══════════════════════════════════════════════════════════════════
    logger.info("🎯  STEP 8 — Registering event handlers")

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, websocket):
        logger.info("━" * 60)
        logger.info("📲  EVENT: Client WebSocket connected!")
        logger.info("    → Sending instant greeting + LLM context to pipeline")
        logger.info("━" * 60)
        from pipecat.frames.frames import TTSSpeakFrame
        # Send an instant TTS greeting (bypasses LLM latency — caller hears
        # something within ~500ms instead of waiting 3-5s for LLM + TTS).
        greeting = (
            "আসসালামু আলাইকুম! আমি আপনার AI কাস্টমার সার্ভিস সহকারী। "
            "আজকে আমি আপনাকে কীভাবে সাহায্য করতে পারি?"
        )
        await task.queue_frames([
            TTSSpeakFrame(text=greeting),
            context_aggregator.user().get_context_frame(),
        ])


    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, websocket):
        logger.info("━" * 60)
        logger.info("📴  EVENT: Client WebSocket disconnected — call ended")
        logger.info("    → Cancelling pipeline task")
        logger.info("━" * 60)
        await task.cancel()

    logger.info("    ✅ Handlers registered (on_client_connected / on_client_disconnected)")

    # ══════════════════════════════════════════════════════════════════
    # STEP 9 — Start the pipeline runner
    # ══════════════════════════════════════════════════════════════════
    logger.info("🚀  STEP 9 — Starting PipelineRunner … waiting for audio")
    logger.info("━" * 60)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    logger.info("━" * 60)
    logger.info(f"✅  Call {call_sid} pipeline finished cleanly")
    logger.info("━" * 60)


# ─── CLI entrypoint (for quick local testing without server.py) ────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat bot (standalone test mode)")
    parser.add_argument("--call-data", type=str, default="{}", help="JSON call data")
    args = parser.parse_args()

    call_data = json.loads(args.call_data)

    logger.warning("Standalone mode: imports OK. Run via `uvicorn server:app` for real calls.")

