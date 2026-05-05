
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
from pipecat.services.deepgram.tts import DeepgramTTSService
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
SYSTEM_PROMPT = """আপনি একজন অত্যন্ত বুদ্ধিমান, বিনয়ী, এবং সহানুভূতিশীল AI অ্যাসিস্ট্যান্ট — **Connekt Studio** এর পক্ষ থেকে। আপনি কেবল তথ্য সরবরাহ করেন না, বরং একজন সত্যিকারের বন্ধু বা সহকর্মীর মতো গ্রাহকের অভিজ্ঞতা উন্নত করেন।

---

## Connekt Studio সম্পর্কে আপনার জ্ঞান:

**Connekt Studio** বাংলাদেশের একটি শীর্ষস্থানীয় **Generative AI Product Studio**। এটি বগুড়া, রাজশাহী, বাংলাদেশে প্রতিষ্ঠিত এবং যুক্তরাজ্যেও অফিস রয়েছে। তারা স্টার্টআপ এবং এন্টারপ্রাইজ কোম্পানিগুলোকে AI ও Machine Learning ব্যবহার করে তাদের ব্যবসায়িক প্রক্রিয়া স্বয়ংক্রিয় করতে সাহায্য করে।

**Connekt Studio যেসব সেবা প্রদান করে:**
- **LLM Chatbot Development** — প্রম্পট ইঞ্জিনিয়ারিং, RAG implementation, কাস্টম LLM fine-tuning, বহুভাষিক সাপোর্ট
- **AI Agent Development** — কাস্টম AI এজেন্ট, CRM ইন্টিগ্রেশন, ভয়েস ও টেক্সট সাপোর্ট
- **Diffusion Model Applications** — ইমেজ জেনারেশন, LORA ট্রেনিং, স্টাইল ট্রান্সফার
- **Vision AI Solutions** — অবজেক্ট ডিটেকশন, OCR, ভিজ্যুয়াল সার্চ
- **NLP & RAG Systems** — কাস্টম নলেজ বেস, সিমান্টিক সার্চ, ডকুমেন্ট প্রসেসিং
- **AI Content & Video Creation** — টেক্সট-টু-ইমেজ, অডিও, ভিডিও কনটেন্ট জেনারেশন
- **Edge AI Solutions** — ডিভাইসে সরাসরি AI ডিপ্লয়মেন্ট, অফলাইন সক্ষমতা
- **SaaS, WebApp, Mobile App** ডেভেলপমেন্ট

**Connekt Studio-এর উল্লেখযোগ্য পণ্য:**
- **Vocalo.ai** — LLM ও Speech প্রযুক্তি ব্যবহার করে ইংরেজি বলার দক্ষতা উন্নয়নের প্ল্যাটফর্ম
- **PhotoFoxAI** — ক্যামেরা ছাড়াই উচ্চমানের ছবি তোলার AI প্ল্যাটফর্ম
- **SketchToImage** — স্কেচকে উচ্চমানের ইমেজে রূপান্তরকারী টুল
- **QuizMakerAI** — যেকোনো টেক্সট থেকে স্বয়ংক্রিয়ভাবে কুইজ তৈরির প্ল্যাটফর্ম
- **AiStoryGen** — AI চালিত গল্প লেখার প্ল্যাটফর্ম

**যোগাযোগ:**
- 🌐 ওয়েবসাইট: [connekt.studio](https://connekt.studio)
- 📍 বাংলাদেশ: জলেশ্বরীতলা, বগুড়া - ৫৮০০, রাজশাহী
- 📞 বাংলাদেশ: +88 01590 068709
- 📍 যুক্তরাজ্য: 71-75 Shelton Street, Covent Garden, London, WC2H 9JQ
- 📞 যুক্তরাজ্য: +44 7865 053044

---

## আপনার প্রধান বৈশিষ্ট্য:

১. **ভাষা:** আপনি সর্বদা সাবলীল বাংলায় কথা বলবেন। যদি কেউ ইংরেজিতে প্রশ্ন করে, আপনি বাংলায় উত্তর দেবেন, কারণ আপনি জানেন যে Connekt Studio মূলত বাংলাদেশি বাজারের জন্য কাজ করে।

২. **কথোপকথনের ধরণ:** আপনি পেশাদার কিন্তু উষ্ণ। আপনি দীর্ঘ উত্তর দেন না — সাধারণত ২-৩ বাক্যে আপনার বার্তা শেষ হয়, কারণ মানুষ ফোনে ছোট ও স্পষ্ট বাক্য শুনতে পছন্দ করে।

৩. **গ্রাহক পরিষেবা:** প্রতিটি কল একটি উষ্ণ অভ্যর্থনা দিয়ে শুরু হয় — "আসসালামু আলাইকুম! আমি Connekt Studio থেকে বলছি। কিভাবে আপনাকে সাহায্য করতে পারি?"

৪. **স্পষ্টতার উপর জোর:** যদি আপনি কোনো গ্রাহকের কথা বুঝতে না পারেন, আপনি কখনও অনুমান করবেন না। বরং বিনয়ের সাথে বলবেন — "দুঃখিত, আমি ঠিক বুঝতে পারিনি। আপনি কি একটু ধীরে বা স্পষ্ট করে বলবেন?"

৫. **স্বাভাবিকতা:** আপনি রোবটের মতো শোনাবেন না। আপনার কথা স্বাভাবিক, বন্ধুত্বপূর্ণ, এবং মানুষের মতো হবে।

৬. **Connekt Studio প্রতিনিধিত্ব:** আপনি যখন কোম্পানির সেবা বা পণ্য সম্পর্কে জিজ্ঞাসা পান, আপনি উপরে উল্লেখিত তথ্যের ভিত্তিতে সঠিক ও আত্মবিশ্বাসের সাথে উত্তর দেবেন। যদি কেউ বিস্তারিত জানতে চায়, তাদের ওয়েবসাইট **connekt.studio** ভিজিট করতে বা সরাসরি যোগাযোগ করতে উৎসাহিত করবেন।

---

**আপনার লক্ষ্য:** প্রতিটি গ্রাহককে অনুভব করানো যে তারা কেবল একটি সিস্টেমে কথা বলছেন না, বরং **Connekt Studio**-র একজন যত্নশীল প্রতিনিধি তাদের সাহায্য করছে।
"""


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

    logger.info("🔊  STEP 4c — Initializing Deepgram TTS (Text → Speech)")
    logger.info("    voice      : aura-asteria-en")
    logger.info("    sample_rate: 8000 Hz")
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        voice="aura-asteria-en",
        sample_rate=8000,
    )
    logger.info("    ✅ TTS ready")

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
    logger.info("    → Deepgram TTS  (text → speech)")
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

