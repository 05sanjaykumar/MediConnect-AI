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
from agents.tools import WEB_CALLER_PREFIX, VoiceSession
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


def _national_number(phone: str) -> str | None:
    """The last ten digits of a number, which is how patients are stored.

    Returns None when there aren't ten digits to take, so a synthetic browser
    id ("web-1a2b3c4d") can't accidentally match a patient.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def identify_caller(phone: str | None) -> VoiceSession:
    """Work out who is calling, from their number.

    On a phone call the number arrives with the call (caller ID), so a patient
    is never asked for it: a known number is greeted by name, an unknown one is
    asked for a name only. The browser has no caller ID, so it gets a synthetic
    one and behaves exactly like a call from an unknown number. Pass ?phone= to
    simulate a known caller instead.
    """
    if not phone:
        phone = f"{WEB_CALLER_PREFIX}{uuid.uuid4().hex[:8]}"
        logger.info(f"Browser call with no caller ID; using synthetic id {phone}")
        return VoiceSession(caller_phone=phone)

    # Store the national number, not the raw caller ID. A phone call arrives as
    # E.164 ("+919840012346") but patients are keyed on the ten digits they
    # would actually type at sign-in — and a record created during this call is
    # written from `caller_phone`. Storing "+91…" made accounts that worked on
    # the call and then failed at the login screen.
    session = VoiceSession(caller_phone=_national_number(phone) or phone)

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.phone == phone))
        if user is None:
            # A real call carries E.164 ("+918072348791"); patients are stored
            # as the ten digits they'd actually give you ("9840012346"). Match
            # on the national number so a phone caller is recognised at all.
            national = _national_number(phone)
            if national:
                user = db.scalar(select(User).where(User.phone == national))
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


def build_pipeline(transport, session: VoiceSession, vad_analyzer=None) -> PipelineTask:
    """Assemble the pipeline. Shared by every transport.

    `vad_analyzer` is supplied by transports whose client does not report
    speech itself — a phone call, where only audio arrives. The browser client
    runs its own detection, so it passes nothing.
    """
    stt = get_stt_service()
    llm = get_llm_service()
    tts = get_tts_service()
    context = get_llm_context(session, vad_analyzer=vad_analyzer)

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
            # No vad_analyzer here: the pipecat JS client runs voice activity
            # detection in the page and sends the speech events with the audio.
            # (These params used to carry vad_enabled/vad_analyzer, which this
            # version of FastAPIWebsocketParams does not define and silently
            # dropped.) The phone transport, which gets no such events, builds
            # its own analyzer — see routes/telephony.py.
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

    # Without this the pipeline keeps running after the caller hangs up:
    # PipelineRunner.run() never returns, so the finally block below never
    # fires and the call stays in ACTIVE_CALLS forever. That matters because
    # ACTIVE_CALLS is what a doctor's status change looks through to find the
    # calls it affects — a stale entry is a frame pushed into a dead pipeline.
    @transport.event_handler("on_client_disconnected")
    async def _hangup(transport, websocket):
        logger.info("Caller disconnected; ending the pipeline")
        await task.cancel()

    call_id = f"web:{id(websocket)}"
    ACTIVE_CALLS[call_id] = (session, task)
    logger.info(f"Call {call_id} started ({len(ACTIVE_CALLS)} active)")

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        ACTIVE_CALLS.pop(call_id, None)
        logger.info(f"Call {call_id} ended ({len(ACTIVE_CALLS)} active)")
