# backend/routes/telephony.py
"""The phone channel.

A real call reaches the same agent the browser does. Twilio answers the call,
asks this service what to do (the TwiML webhook), and then opens a WebSocket
carrying the audio both ways. From `build_pipeline` onwards nothing differs
from a browser call — only the transport and the serializer change, which is
why `routes/audio.py` kept them as parameters.

Two things about telephony audio are not negotiable and drive the setup below:

* It is 8 kHz mu-law, not the 16 kHz PCM the browser sends. The serializer
  handles the encoding; the sample rates have to be declared on the transport
  so the STT and TTS services resample to and from it.
* There is no client-side voice activity detection. The browser runs VAD in
  the page and tells us when the caller starts and stops speaking; a telephone
  tells us nothing. Without a server-side analyzer the agent would never
  decide the caller had finished, so the phone path supplies its own.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pydantic import BaseModel, Field
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from agents.prompt import greeting_for
from config import (
    PUBLIC_HOST,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    USER_SPEECH_TIMEOUT,
)
from deps import require_role
from routes.audio import ACTIVE_CALLS, build_pipeline, identify_caller

router = APIRouter(prefix="/twilio", tags=["telephony"])

TELEPHONY_SAMPLE_RATE = 8000

# The caller's number is handed to the WebSocket as a <Parameter> on the
# stream, because the media stream itself carries only the stream and call sids.
CALLER_PARAM = "caller"


def _require_config(*, need_number: bool = False) -> None:
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("PUBLIC_HOST", PUBLIC_HOST),
        )
        if not value
    ]
    if need_number and not TWILIO_PHONE_NUMBER:
        missing.append("TWILIO_PHONE_NUMBER")
    if missing:
        raise HTTPException(
            503,
            "Telephony is not configured. Missing in backend/.env: "
            + ", ".join(missing),
        )


def _public_host() -> str:
    """The host Twilio reaches us on — an ngrok host in development."""
    return (PUBLIC_HOST or "").replace("https://", "").replace("http://", "").rstrip("/")


def _twilio_client() -> Client:
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _caller_id(value: str) -> str:
    """Normalise whatever Twilio put in From/To into something identifiable.

    A PSTN call gives a plain number. A SIP call gives a URI —
    `sip:9840012346@mediconnect-097e72.sip.twilio.com` — and the host half is
    poison: it contains digits, so handing the whole URI to the caller lookup
    would mix the domain's digits into the phone number and match nobody.
    Take the user part and leave the rest.
    """
    value = (value or "").strip()
    if value.lower().startswith(("sip:", "sips:")):
        value = value.split(":", 1)[1]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


# --------------------------------------------------------------- the webhook


@router.post("/voice", response_class=PlainTextResponse)
async def voice_webhook(request: Request):
    """Twilio asks what to do with a call; we tell it to open a media stream.

    `<Connect><Stream>` is bidirectional and holds the call open for as long as
    the WebSocket lives, which is what lets the agent both listen and speak.

    Who the patient is depends on which way the call went. On an inbound call
    the patient dialled us, so they are `From`. On a call we placed, we dialled
    them, so they are `To` — and `From` is our own Twilio number, which would
    identify nobody.
    """
    form = await request.form()
    direction = str(form.get("Direction", "inbound"))
    raw = str(form.get("To" if direction.startswith("outbound") else "From", ""))
    caller = _caller_id(raw)
    call_sid = str(form.get("CallSid", ""))
    channel = "sip" if raw.lower().startswith(("sip:", "sips:")) else "pstn"
    logger.info(f"TwiML requested: {direction} {channel} call {call_sid} with {caller}")

    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"wss://{_public_host()}/api/twilio/stream")
    # Survives into the WebSocket as start.customParameters.
    stream.parameter(name=CALLER_PARAM, value=caller)
    response.append(connect)
    return str(response)


# ---------------------------------------------------------- the media stream


async def _read_stream_start(websocket: WebSocket) -> dict:
    """Consume Twilio's two opening messages and return the `start` payload.

    Twilio sends `connected` and then `start` before any audio. The serializer
    needs the stream sid from `start` to address audio back at the call, so the
    pipeline cannot be built until both have arrived.
    """
    messages = websocket.iter_text()
    await messages.__anext__()                       # {"event": "connected"}
    start = json.loads(await messages.__anext__())   # {"event": "start", ...}
    return start.get("start", {})


@router.websocket("/stream")
async def twilio_stream(websocket: WebSocket):
    await websocket.accept()

    start = await _read_stream_start(websocket)
    stream_sid = start.get("streamSid")
    call_sid = start.get("callSid")
    caller = (start.get("customParameters") or {}).get(CALLER_PARAM)

    if not stream_sid:
        logger.error("Twilio stream started without a streamSid; closing")
        await websocket.close()
        return

    session = identify_caller(caller)

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=TWILIO_ACCOUNT_SID,
        auth_token=TWILIO_AUTH_TOKEN,
        # Hang the call up when the pipeline ends, so a finished conversation
        # doesn't leave the line open and billing.
        params=TwilioFrameSerializer.InputParams(auto_hang_up=True),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            serializer=serializer,
        ),
    )

    # A phone sends no speech events, so the turn has to be detected here.
    # stop_secs matches the speech timeout the LLM aggregator already uses, so
    # the phone behaves like the browser rather than feeling more impatient.
    vad = SileroVADAnalyzer(params=VADParams(stop_secs=USER_SPEECH_TIMEOUT))

    task = build_pipeline(transport, session, vad_analyzer=vad)

    @transport.event_handler("on_client_connected")
    async def _greet(transport, websocket):
        await task.queue_frames([TTSSpeakFrame(greeting_for(session))])

    @transport.event_handler("on_client_disconnected")
    async def _hangup(transport, websocket):
        logger.info(f"Caller hung up ({call_sid}); ending the pipeline")
        await task.cancel()

    call_id = f"tel:{call_sid or stream_sid}"
    ACTIVE_CALLS[call_id] = (session, task)
    logger.info(f"Call {call_id} started from {caller} ({len(ACTIVE_CALLS)} active)")

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        ACTIVE_CALLS.pop(call_id, None)
        logger.info(f"Call {call_id} ended ({len(ACTIVE_CALLS)} active)")


# ------------------------------------------------------------ outbound calls


class OutboundCall(BaseModel):
    to: str = Field(
        description="The number to call, in E.164 form, e.g. +919840012346.",
        examples=["+918072348791"],
    )


@router.post("/call", dependencies=[Depends(require_role("admin"))])
def place_call(body: OutboundCall):
    """Ring a patient and connect them to the agent.

    This is the demonstration path: a trial Twilio account may only call
    numbers that have been verified in the console, so the phone being called
    is normally the one sitting on the desk.

    Admin-only on purpose — it spends real money on every request.
    """
    _require_config(need_number=True)

    client = _twilio_client()
    try:
        call = client.calls.create(
            to=body.to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"https://{_public_host()}/api/twilio/voice",
        )
    except Exception as exc:  # twilio raises its own error types
        logger.error(f"Outbound call to {body.to} failed: {exc}")
        raise HTTPException(502, f"Twilio refused the call: {exc}")

    logger.info(f"Outbound call {call.sid} placed to {body.to}")
    return {"call_sid": call.sid, "to": body.to, "from": TWILIO_PHONE_NUMBER,
            "status": call.status}


@router.get("/status")
def telephony_status():
    """What telephony needs, and what is actually configured."""
    host = _public_host()
    return {
        "configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and host
                           and TWILIO_PHONE_NUMBER),
        "account_sid": bool(TWILIO_ACCOUNT_SID),
        "auth_token": bool(TWILIO_AUTH_TOKEN),
        "twilio_number": TWILIO_PHONE_NUMBER or None,
        "public_host": host or None,
        "webhook_url": f"https://{host}/api/twilio/voice" if host else None,
        "stream_url": f"wss://{host}/api/twilio/stream" if host else None,
        "active_calls": len(ACTIVE_CALLS),
    }
