# backend/config.py
"""Voice-service configuration.

Model names are read from the environment so they can be changed without
touching code — useful when a provider retires a model, which is exactly what
happened to the Llama line this project originally used.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------------ LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# openai/gpt-oss-120b handles multi-step tool calls well, which is what booking
# needs (search a doctor -> list slots -> confirm). Drop to openai/gpt-oss-20b
# if the pause before the agent speaks feels too long on a phone call.
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

# gpt-oss models think before answering. "low" keeps that short, which matters
# when someone is waiting on the line.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")

# How long the caller must pause before we treat their turn as finished.
# Too short cuts people off mid-sentence; too long feels like the agent is
# ignoring them. 0.8s is a reasonable phone-call pause.
USER_SPEECH_TIMEOUT = float(os.getenv("USER_SPEECH_TIMEOUT", "0.8"))

# ------------------------------------------------------------------------ STT
# NVIDIA streams transcripts as the caller speaks. Groq's whisper-large-v3-turbo
# is a batch API — a usable fallback, but it waits for the utterance to finish.
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# ------------------------------------------------------------------------ TTS
# Kokoro runs locally — no key, but needs espeak-ng installed.
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")

# ------------------------------------------------------------------ telephony
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_HOST = os.getenv("PUBLIC_HOST")  # ngrok host for Twilio webhooks

# ------------------------------------------------------------------------ app
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
