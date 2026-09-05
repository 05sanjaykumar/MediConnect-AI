# backend/routes/audio.py
"""The voice channel.

One pipeline, built once, so a browser call and (later) a phone call differ
only in their transport and serializer. Each connection gets its own
VoiceSession — that is what ties a booking to a caller, and what stops the
model booking a slot it was never offered.
"""

import uuid

from fastapi import APIRouter, Query, WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.frames.frames import Frame, InterruptionFrame, TTSSpeakFrame
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from sqlalchemy import select

from agents.prompt import greeting_for
from agents.tools import VoiceSession
from database import SessionLocal
from models import User
from services.LLM import get_llm_context, get_llm_service
from services.STT import get_stt_service
from services.TTS import get_tts_service

router = APIRouter()


class BrowserFrameSerializer(ProtobufFrameSerializer):
    """The stock serializer, minus one frame the browser can't read.

    pipecat-ai 1.8 added `interruption` to the protobuf schema. The JS
    transport — even at its latest release — only knows text, audio,
    transcription and message, so every interruption produced a red
    "Unknown frame kind" in the console. The browser handles interruption
    itself by watching the audio stream, so the frame carries nothing it
    needs; dropping it loses nothing.
    """

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, InterruptionFrame):
            return None
        return await super().serialize(frame)

# Every live call, so a doctor's status change can find the ones it affects.
# Keyed by session id. This is what the mid-call interrupt will reach into.
ACTIVE_CALLS: dict[str, tuple[VoiceSession, PipelineTask]] = {}


@router.get("/health")
def health():
    return {"status": "ok", "active_calls": len(ACTIVE_CALLS)}


def identify_caller(phone: str | None) -> VoiceSession:
    """Work out who is calling, from their number.

    On a phone call the number arrives with the call (caller ID), so a patient
    is never asked for it: a known number is greeted by name, an unknown one is
    asked for a name only. The browser has no caller ID, so it gets a synthetic
    one and behaves exactly like a call from an unknown number. Pass ?phone= to
    simulate a known caller instead.
    """
    if not phone:
        phone = f"web-{uuid.uuid4().hex[:8]}"
        logger.info(f"Browser call with no caller ID; using synthetic id {phone}")
        return VoiceSession(caller_phone=phone)

    session = VoiceSession(caller_phone=phone)

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.phone == phone))
        if user is not None:
            session.patient_id = user.id
            session.patient_name = user.name
            session.known_at_start = True
            logger.info(f"Voice call from {user.name} ({phone})")
        else:
            logger.info(f"Voice call from an unrecognised number: {phone}")
    finally:
        db.close()
    return session


def build_pipeline(transport, session: VoiceSession) -> PipelineTask:
    """Assemble the pipeline. Shared by every transport."""
    stt = get_stt_service()
    llm = get_llm_service()
    tts = get_tts_service()
    context = get_llm_context(session)

    pipeline = Pipeline([
        transport.input(),
        stt,
        context.user(),
        llm,
        tts,
        context.assistant(),
        transport.output(),
    ])

    return PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))


@router.websocket("/ws/voice")
async def voice_websocket(
    websocket: WebSocket,
    phone: str | None = Query(
        default=None,
        description="The caller's number. Identifies the patient a booking belongs to.",
    ),
):
    await websocket.accept()

    session = identify_caller(phone)

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.5)),
            vad_audio_passthrough=False,
            serializer=BrowserFrameSerializer(),
        ),
    )

    task = build_pipeline(transport, session)

    # Speak first. The greeting goes straight to TTS — no model call — so the
    # caller hears a voice within a second of connecting, by name if we know
    # them. The same line is already in the LLM context as the assistant's
    # opening, so the model won't introduce itself a second time.
    @transport.event_handler("on_client_connected")
    async def _greet(transport, websocket):
        await task.queue_frames([TTSSpeakFrame(greeting_for(session))])

    call_id = f"web:{id(websocket)}"
    ACTIVE_CALLS[call_id] = (session, task)
    logger.info(f"Call {call_id} started ({len(ACTIVE_CALLS)} active)")

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        ACTIVE_CALLS.pop(call_id, None)
        logger.info(f"Call {call_id} ended ({len(ACTIVE_CALLS)} active)")
