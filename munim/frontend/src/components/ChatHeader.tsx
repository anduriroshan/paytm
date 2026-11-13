// Who the chat is with, and where the case stands. On a new chat the name is a picker (which demo merchant is typing).
import { Check, ChevronDown, Clock, Flag, Hourglass, LoaderCircle, X } from "lucide-react";
import { langLabel } from "../format";
import { initials, type Tone } from "../lib/explain";
import type { MerchantInfo } from "../types";

export interface StatusPill {
  tone: Tone;
  text: string;
  title: string;
}

interface Props {
  merchant: MerchantInfo | null;
  picker: { options: MerchantInfo[]; value: string; onPick: (id: string) => void } | null;
  pill: StatusPill | null;
  onPill: () => void;
}

const PILL_CLASS: Record<Tone, string> = { run: "run", ok: "ok", hum: "hum", esc: "warn", bad: "bad", gray: "gray" };

function PillIcon({ tone }: { tone: Tone }) {
  switch (tone) {
    case "run":
      return <LoaderCircle className="ico spin" aria-hidden />;
    case "ok":
      return <Check className="ico" aria-hidden />;
    case "hum":
      return <Hourglass className="ico" aria-hidden />;
    case "esc":
      return <Flag className="ico" aria-hidden />;
    case "bad":
      return <X className="ico" aria-hidden />;
    default:
      return <Clock className="ico" aria-hidden />;
  }
}

export function ChatHeader({ merchant, picker, pill, onPill }: Props) {
  if (!merchant) return <header className="chathead" />;
  const sub = [merchant.owner_name, merchant.city, merchant.language ? `speaks ${langLabel(merchant.language)}` : ""].filter(Boolean).join(" · ");
  return (
    <header className="chathead">
      <span className="av">{initials(merchant.business_name)}</span>
      <div>
        {picker ? (
          <label className="picker" title="Which demo merchant is chatting">
            <select value={picker.value} onChange={(e) => picker.onPick(e.target.value)} aria-label="Merchant">
              {picker.options.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.business_name}
                </option>
              ))}
            </select>
            <ChevronDown className="ico" aria-hidden />
          </label>
        ) : (
          <b>{merchant.business_name}</b>
        )}
        <small>{sub}</small>
      </div>
      <span className="spacer" />
      {pill && (
        <button type="button" className={`pill ${PILL_CLASS[pill.tone]}`} onClick={onPill} title={pill.title}>
          <PillIcon tone={pill.tone} />
          {pill.text}
        </button>
      )}
    </header>
  );
}
