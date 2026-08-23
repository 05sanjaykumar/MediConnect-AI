# backend/main.py
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.audio import router as audio_router
from dotenv import load_dotenv

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Gensail Voice AI Backend starting...")
    yield
    print("🛑 Shutting down...")

app = FastAPI(
    title="Gensail Voice AI",
    version="1.0.0",
    lifespan=lifespan
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audio_router, prefix="/api")

@app.get("/")
def root():
    return {"status": "running 🚀", "version": "1.0.0"}