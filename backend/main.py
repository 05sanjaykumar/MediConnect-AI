# backend/main.py
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import describe
from routes.admin import router as admin_router
from routes.appointments import router as appointments_router
from routes.auth import router as auth_router
from routes.doctors import router as doctors_router

load_dotenv()

# The voice pipeline pulls in Pipecat, Kokoro and friends. Anyone working on
# booking or the dashboards can run the backend without installing all that —
# the API stays up, only the voice endpoints go missing.
try:
    from routes.audio import router as audio_router

    VOICE_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - depends on local install
    audio_router = None
    VOICE_AVAILABLE = False
    VOICE_IMPORT_ERROR = str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 MediConnect AI backend starting...")
    print(f"   database: {describe()}")
    print(f"   voice:    {'pipecat loaded' if VOICE_AVAILABLE else 'disabled (pipecat not installed)'}")
    # Schema is owned by Alembic. Run `alembic upgrade head` after pulling
    # changes; the app never alters tables on startup.
    yield
    print("🛑 Shutting down...")


app = FastAPI(
    title="MediConnect AI",
    description="Voice-AI driven doctor appointment and live availability platform.",
    version="0.2.0",
    lifespan=lifespan,
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if VOICE_AVAILABLE:
    app.include_router(audio_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(doctors_router, prefix="/api")
app.include_router(appointments_router, prefix="/api")
app.include_router(admin_router, prefix="/api")


@app.get("/")
def root():
    return {
        "status": "running 🚀",
        "service": "MediConnect AI",
        "version": "0.2.0",
        "voice_enabled": VOICE_AVAILABLE,
    }
