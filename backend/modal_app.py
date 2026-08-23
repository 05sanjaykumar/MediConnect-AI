import modal

app = modal.App("gensail-voice-agent")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "libsndfile1",
        "espeak-ng",
        "espeak-ng-data",
    )
    .pip_install(
        "fastapi",
        "uvicorn",
        "python-multipart",
        "pydantic",
        "python-dotenv",
        "pipecat-ai[kokoro,nvidia,silero,websocket]",
        "groq",
        "soundfile",
        "numpy>=2.0.2,<3.0.0",
    )
    .add_local_python_source("main", "config", "routes", "services")
)

@app.function(
    image=image,
    gpu="A10G",
    secrets=[modal.Secret.from_name("gensail-secrets")],
    timeout=600,
    single_use_containers=True,
    min_containers=0,
)
@modal.asgi_app()
def fastapi_app():
    from main import app as fa
    return fa