// frontend/app/page.tsx

"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import {
  WebSocketTransport,
  ProtobufFrameSerializer,
} from "@pipecat-ai/websocket-transport";

type Status = "idle" | "connecting" | "listening" | "speaking" | "error";

export default function Home() {
  const clientRef = useRef<PipecatClient | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [transcript, setTranscript] = useState("");
  const [botReply, setBotReply] = useState("");
  const [error, setError] = useState("");

  const isConnectedRef = useRef(false);

  const WS_URL =
    process.env.NEXT_PUBLIC_BACKEND_WS_URL || "ws://localhost:8000/api/ws/voice";

  const initClient = useCallback(() => {
    const client = new PipecatClient({
      transport: new WebSocketTransport({
        serializer: new ProtobufFrameSerializer(),
        recorderSampleRate: 24000,
        playerSampleRate: 24000,
      }),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onConnected: () => {
          isConnectedRef.current = true;
          setStatus("listening");
        },
        onDisconnected: () => {
          isConnectedRef.current = false;
          setStatus("idle");
        },
        onError: (err) => {
          setError(String(err));
          setStatus("error");
        },
        onBotStartedSpeaking: () => setStatus("speaking"),
        onBotStoppedSpeaking: () => setStatus("listening"),
        onUserStartedSpeaking: () => setTranscript(""),
        onUserTranscript: (data) => {
          if (data.final) setTranscript(data.text);
        },
        onBotTranscript: (data) => setBotReply(data.text),
      },
    });

    clientRef.current = client;
  }, []);

  useEffect(() => {
    initClient();
    return () => {
      clientRef.current?.disconnect().catch(() => {
        // no-op: session was never started, safe to ignore
      });
    };
  }, [initClient]);


  const handleConnect = async () => {
    try {
      setStatus("connecting");
      setError("");
      await clientRef.current?.connect({ wsUrl: WS_URL });
    } catch (e) {
      setError(String(e));
      setStatus("error");
    }
  };

  const handleDisconnect = async () => {
  try {
    await clientRef.current?.disconnect();
  } catch {
    // ignore if session wasn't active
  }
  isConnectedRef.current = false;
  setStatus("idle");
  setTranscript("");
  setBotReply("");
};

  const isActive = status === "listening" || status === "speaking";

  return (
    <main className="min-h-screen bg-[#0f0f0f] text-white flex flex-col items-center justify-center px-4">
      {/* Header */}
      <div className="mb-12 text-center">
        <h1 className="text-3xl font-semibold tracking-tight text-white">
          Voice AI
        </h1>
        <p className="text-sm text-zinc-500 mt-1">
          Low-latency voice agent · Single environment
        </p>
      </div>

      {/* Mic Button */}
      <button
        onClick={isActive ? handleDisconnect : handleConnect}
        disabled={status === "connecting"}
        className={`
          relative w-24 h-24 rounded-full flex items-center justify-center
          transition-all duration-300 cursor-pointer
          ${status === "idle" || status === "error"
            ? "bg-zinc-800 hover:bg-zinc-700 border border-zinc-600"
            : status === "connecting"
            ? "bg-zinc-800 border border-zinc-600 opacity-60 cursor-not-allowed"
            : status === "listening"
            ? "bg-red-600 hover:bg-red-500 shadow-[0_0_32px_rgba(220,38,38,0.4)]"
            : "bg-green-600 hover:bg-green-500 shadow-[0_0_32px_rgba(34,197,94,0.4)]"
          }
        `}
      >
        {/* Pulse ring when active */}
        {isActive && (
          <span className={`
            absolute inset-0 rounded-full animate-ping opacity-20
            ${status === "listening" ? "bg-red-500" : "bg-green-500"}
          `} />
        )}

        {/* Icon */}
        {status === "connecting" ? (
          <svg className="w-8 h-8 animate-spin text-zinc-400" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
          </svg>
        ) : (
          <svg className="w-8 h-8" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 1a4 4 0 014 4v6a4 4 0 01-8 0V5a4 4 0 014-4zm-1 17.93V21h2v-2.07A8.001 8.001 0 0020 11h-2a6 6 0 01-12 0H4a8.001 8.001 0 007 7.93z"/>
          </svg>
        )}
      </button>

      {/* Status Label */}
      <p className={`mt-6 text-sm font-medium tracking-wide uppercase ${
        status === "idle" ? "text-zinc-500" :
        status === "connecting" ? "text-zinc-400" :
        status === "listening" ? "text-red-400" :
        status === "speaking" ? "text-green-400" :
        "text-red-400"
      }`}>
        {status === "idle" && "Click to start"}
        {status === "connecting" && "Connecting..."}
        {status === "listening" && "Listening"}
        {status === "speaking" && "Speaking"}
        {status === "error" && "Connection error"}
      </p>

      {/* Transcript Area */}
      <div className="mt-10 w-full max-w-md space-y-3">
        {transcript && (
          <div className="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-3">
            <p className="text-xs text-zinc-500 mb-1 uppercase tracking-wide">You said</p>
            <p className="text-sm text-zinc-200">{transcript}</p>
          </div>
        )}
        {botReply && (
          <div className="bg-zinc-900 border border-zinc-700 rounded-xl px-4 py-3">
            <p className="text-xs text-green-500 mb-1 uppercase tracking-wide">AI Assistant</p>
            <p className="text-sm text-white">{botReply}</p>
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <p className="mt-4 text-xs text-red-400 max-w-sm text-center">{error}</p>
      )}

      {/* Footer */}
      <p className="absolute bottom-6 text-xs text-zinc-700">
        Pipecat · Kokoro TTS · NVIDIA Nemotron STT
      </p>
    </main>
  );
}