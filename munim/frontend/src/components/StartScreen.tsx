// New chat: the teammate brings in a merchant's complaint (typed, pasted or spoken), or picks an example.
import { useState } from "react";
import { RotateCcw } from "lucide-react";
import { langTag } from "../lib/explain";
import { START_CHIPS, START_CHIPS_SHOWN } from "../lib/suggestions";
import type { Scenario } from "../types";
import { Composer } from "./Composer";

interface Props {
  firstName: string;
  draft: string;
  onDraft: (v: string) => void;
  onSend: (text: string, channel: "chat" | "voice") => void;
  scenarios: Scenario[];
  onChip: (s: Scenario) => void;
  backend: "checking" | "online" | "offline" | "skipped";
  onRetry: () => void;
  voice: boolean;
  busy: boolean;
  error: string | null;
}

export function StartScreen({ firstName, draft, onDraft, onSend, scenarios, onChip, backend, onRetry, voice, busy, error }: Props) {
  const [more, setMore] = useState(false);
  const chips = START_CHIPS.map((c) => ({ ...c, s: scenarios.find((s) => s.id === c.scenario) })).filter((c): c is typeof c & { s: Scenario } => !!c.s);
  const shown = more ? chips : chips.slice(0, START_CHIPS_SHOWN);
  const offline = backend === "offline";

  return (
    <div className="hello">
      <div className="big">M</div>
      <h1>What did {firstName || "the merchant"} say?</h1>
      <p>Paste or speak their complaint, in any Indian language.</p>
      <Composer
        value={draft}
        onChange={onDraft}
        onSend={onSend}
        placeholder={offline ? "The backend is offline" : backend === "checking" ? "Connecting…" : "Paste or speak the merchant's complaint…"}
        disabled={backend !== "online"}
        busy={busy}
        voice={voice}
        autoFocus
      />
      {offline && (
        <div className="cnote">
          The backend is offline, so the examples below play recorded cases.{" "}
          <button type="button" className="link" onClick={onRetry}>
            <RotateCcw className="ico" aria-hidden />
            Retry
          </button>
        </div>
      )}
      {error && <div className="cnote bad">{error}</div>}
      <div className="sugg">
        <small>Try</small>
        {shown.map((c) => (
          <button key={c.scenario} type="button" onClick={() => onChip(c.s)} disabled={busy || backend === "checking"} title={c.s.complaint_text}>
            <i className="lang">{langTag(c.s.language)}</i>
            {c.label}
          </button>
        ))}
        {chips.length > START_CHIPS_SHOWN && (
          <button type="button" className="more" onClick={() => setMore((m) => !m)}>
            {more ? "Less" : `${chips.length - START_CHIPS_SHOWN} more`}
          </button>
        )}
      </div>
    </div>
  );
}
