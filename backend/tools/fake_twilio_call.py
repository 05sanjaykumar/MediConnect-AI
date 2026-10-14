#!/usr/bin/env python
"""Place a fake Twilio call against a running server.

Twilio's media stream is a plain WebSocket speaking a small JSON protocol, so
the whole inbound path can be exercised from this machine: the handshake, the
caller lookup, the pipeline, and the greeting coming back as 8 kHz mu-law.
That means the phone path can be verified without dialling anything — useful
when the number is in another country and every test call costs the caller
international rates.

    python tools/fake_twilio_call.py --caller +919840012346 --seconds 12

Writes the audio the agent sent back to /tmp/fake_call.wav so it can be played.
"""

import argparse
import asyncio
import audioop
import base64
import json
import time
import wave

import websockets

SAMPLE_RATE = 8000
CHUNK_MS = 20
SAMPLES_PER_CHUNK = SAMPLE_RATE * CHUNK_MS // 1000   # 160 samples = 20 ms
SILENCE = b"\xff" * SAMPLES_PER_CHUNK                # mu-law silence


async def run(url: str, caller: str, seconds: float, speech: str | None,
              speak_after: float = 6.0) -> int:
    stream_sid = f"MZ{int(time.time())}"
    call_sid = f"CA{int(time.time())}"

    async with websockets.connect(url, max_size=None) as ws:
        # Twilio always sends these two before any audio.
        await ws.send(json.dumps({"event": "connected", "protocol": "Call",
                                  "version": "1.0.0"}))
        await ws.send(json.dumps({
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "callSid": call_sid,
                "accountSid": "ACfake",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw",
                                "sampleRate": SAMPLE_RATE, "channels": 1},
                "customParameters": {"caller": caller},
            },
        }))
        print(f"→ stream {stream_sid} opened as {caller}")

        outbound: list[bytes] = []
        stop_at = time.monotonic() + seconds
        inbound_audio = _speech_chunks(speech) if speech else None

        async def feed():
            """Send audio up to the agent, as a phone would — continuously."""
            seq = 2
            # Let the greeting finish before speaking, or the agent is talked
            # over and the transcript starts mid-sentence.
            speak_at = time.monotonic() + speak_after
            while time.monotonic() < stop_at:
                chunk = SILENCE
                if inbound_audio is not None and time.monotonic() >= speak_at:
                    chunk = next(inbound_audio, SILENCE)
                await ws.send(json.dumps({
                    "event": "media",
                    "sequenceNumber": str(seq),
                    "streamSid": stream_sid,
                    "media": {"track": "inbound", "chunk": str(seq),
                              "timestamp": str(seq * CHUNK_MS),
                              "payload": base64.b64encode(chunk).decode()},
                }))
                seq += 1
                await asyncio.sleep(CHUNK_MS / 1000)

        async def drain():
            """Collect what the agent says back."""
            while time.monotonic() < stop_at:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
                message = json.loads(raw)
                if message.get("event") == "media":
                    outbound.append(base64.b64decode(message["media"]["payload"]))
                elif message.get("event") == "mark":
                    print(f"  ← mark {message.get('mark', {}).get('name')}")

        await asyncio.gather(feed(), drain())

    if not outbound:
        print("✗ the agent sent no audio back")
        return 1

    mulaw = b"".join(outbound)
    pcm = audioop.ulaw2lin(mulaw, 2)
    seconds_of_audio = len(mulaw) / SAMPLE_RATE
    with wave.open("/tmp/fake_call.wav", "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)

    print(f"✓ agent sent {len(mulaw)} mu-law bytes ({seconds_of_audio:.1f}s of speech)")
    print("  saved to /tmp/fake_call.wav — play it with:  afplay /tmp/fake_call.wav")
    return 0


def _speech_chunks(path: str):
    """Read a wav file and hand it out as 20 ms mu-law chunks at 8 kHz."""
    with wave.open(path, "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
        if wav.getnchannels() == 2:
            pcm = audioop.tomono(pcm, wav.getsampwidth(), 0.5, 0.5)
        if wav.getframerate() != SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, wav.getsampwidth(), 1,
                                    wav.getframerate(), SAMPLE_RATE, None)
    mulaw = audioop.lin2ulaw(pcm, 2)
    for i in range(0, len(mulaw), SAMPLES_PER_CHUNK):
        yield mulaw[i:i + SAMPLES_PER_CHUNK].ljust(SAMPLES_PER_CHUNK, b"\xff")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8000/api/twilio/stream")
    parser.add_argument("--caller", default="+919840012346",
                        help="the number the call appears to come from")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--speech", help="a wav file to speak into the call")
    parser.add_argument("--speak-after", type=float, default=6.0,
                        help="seconds to wait before speaking, so the greeting finishes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.url, args.caller, args.seconds, args.speech,
                                 args.speak_after)))
