from config import NVIDIA_API_KEY
from pipecat.services.nvidia.stt import NvidiaSTTService

def get_stt_service():
    return NvidiaSTTService(
        api_key=NVIDIA_API_KEY,
    )