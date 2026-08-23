# backend/routes/audio.py
from fastapi import APIRouter, WebSocket
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from services.STT import get_stt_service
from services.LLM import get_llm_service, get_llm_context
from services.TTS import get_tts_service

router = APIRouter()

@router.get("/health")
def health():
    return {"status": "ok"}

@router.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket):
    await websocket.accept()
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=0.5)
            ),
            vad_audio_passthrough=False,
            serializer=ProtobufFrameSerializer(),
        ),
    )

    stt = get_stt_service()
    llm = get_llm_service()
    tts = get_tts_service()
    context_aggregator = get_llm_context()

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        context_aggregator.assistant(),
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
        ),
    )

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)