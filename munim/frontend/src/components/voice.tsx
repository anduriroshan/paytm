// Voice: "Listen" plays a reply through Sarvam TTS; the mic records a voice note and transcribes it with Sarvam STT.
// Both only render when the backend reports live voice.
import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, Square, Volume2 } from "lucide-react";
import { api } from "../api";

export function SpeakButton({ text, language }: { text: string; language: string | null }) {
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  const onClick = async () => {
    if (state === "playing") {
      audioRef.current?.pause();
      setState("idle");
      return;
    }
    if (state === "loading") return;
    setState("loading");
    try {
      const r = await api.tts(text, language ?? "hi");
      const audio = new Audio(`data:${r.mime || "audio/wav"};base64,${r.audio_base64}`);
      audioRef.current = audio;
      audio.onended = () => setState("idle");
      await audio.play();
      setState("playing");
    } catch {
      setState("idle");
    }
  };

  return (
    <button type="button" className="btn" onClick={onClick} aria-label={state === "playing" ? "Stop" : "Listen"}>
      {state === "loading" ? <LoaderCircle className="ico spin" aria-hidden /> : state === "playing" ? <Square className="ico" aria-hidden /> : <Volume2 className="ico" aria-hidden />}
      {state === "playing" ? "Stop" : "Listen"}
    </button>
  );
}

/** Record → transcribe. `onText` receives the transcript (in the speaker's language). */
export function useVoiceInput(onText: (text: string) => void) {
  const [state, setState] = useState<"idle" | "recording" | "transcribing">("idle");
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(
    () => () => {
      const r = recRef.current;
      if (r && r.state !== "inactive") {
        r.onstop = null;
        r.stop();
        r.stream.getTracks().forEach((t) => t.stop());
      }
    },
    [],
  );

  const toggle = useCallback(async () => {
    setError(null);
    const cur = recRef.current;
    if (cur && cur.state === "recording") {
      cur.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : undefined;
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => {
        if (e.data.size) chunks.push(e.data);
      };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        recRef.current = null;
        setState("transcribing");
        try {
          const r = await api.stt(new Blob(chunks, { type: rec.mimeType || "audio/webm" }), "complaint.webm");
          if (r.text?.trim()) onTextRef.current(r.text.trim());
          else setError("Didn't catch that. Try again.");
        } catch (e) {
          setError(`Couldn't transcribe: ${(e as Error).message}`);
        } finally {
          setState("idle");
        }
      };
      recRef.current = rec;
      rec.start();
      setState("recording");
    } catch {
      setError("Microphone not available.");
      setState("idle");
    }
  }, []);

  return { state, error, toggle };
}
