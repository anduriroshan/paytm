// The one input box: type (Enter sends, Shift+Enter for a new line) or speak (live voice only).
import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Mic, SendHorizontal, Square } from "lucide-react";
import { useVoiceInput } from "./voice";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: (text: string, channel: "chat" | "voice") => void;
  placeholder: string;
  disabled?: boolean;
  busy?: boolean;
  voice?: boolean;
  autoFocus?: boolean;
}

export function Composer({ value, onChange, onSend, placeholder, disabled, busy, voice, autoFocus }: Props) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [fromVoice, setFromVoice] = useState(false);
  const mic = useVoiceInput((text) => {
    onChange(text);
    setFromVoice(true);
    ref.current?.focus();
  });

  // Grow with the text, up to ~5 lines.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 128)}px`;
  }, [value]);

  const canSend = !disabled && !busy && value.trim().length > 0;
  const send = () => {
    if (!canSend) return;
    onSend(value.trim(), fromVoice ? "voice" : "chat");
    setFromVoice(false);
  };

  return (
    <>
      <div className={`cbox${disabled ? " off" : ""}`}>
        <textarea
          ref={ref}
          rows={1}
          value={value}
          placeholder={mic.state === "recording" ? "Listening… tap stop when done" : mic.state === "transcribing" ? "Transcribing…" : placeholder}
          disabled={disabled}
          autoFocus={autoFocus}
          aria-label={placeholder}
          onChange={(e) => {
            onChange(e.target.value);
            if (!e.target.value) setFromVoice(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          className={/[ऀ-෿]/.test(value) ? "ind" : undefined}
        />
        {voice && (
          <button
            type="button"
            className={`btn icon${mic.state === "recording" ? " rec" : ""}`}
            onClick={mic.toggle}
            disabled={disabled || mic.state === "transcribing"}
            aria-label={mic.state === "recording" ? "Stop recording" : "Speak"}
            title={mic.state === "recording" ? "Stop recording" : "Speak in any Indian language"}
          >
            {mic.state === "transcribing" ? <LoaderCircle className="ico spin" aria-hidden /> : mic.state === "recording" ? <Square className="ico" aria-hidden /> : <Mic className="ico" aria-hidden />}
          </button>
        )}
        <button type="button" className="btn pri icon" onClick={send} disabled={!canSend} aria-label="Send">
          {busy ? <LoaderCircle className="ico spin" aria-hidden /> : <SendHorizontal className="ico" aria-hidden />}
        </button>
      </div>
      {mic.error && <div className="cnote bad">{mic.error}</div>}
    </>
  );
}
