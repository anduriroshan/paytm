// Right-hand panel. While Munim works: the 12 steps, live. Afterwards: "How Munim decided" (Steps / Proof / Decision).
import { ArrowRight, Ban, Check, Code, Coins, Flag, Hourglass, LoaderCircle, Search, Shield, Sparkles, Users, X } from "lucide-react";
import type { ReactNode } from "react";
import {
  deriveCause,
  deriveClaims,
  deriveDecision,
  deriveSteps,
  deriveTrail,
  STEP_COUNT,
  type ClaimRow,
  type RanLine,
  type StepIcon,
  type StepRow,
} from "../lib/explain";
import type { CaseView } from "../state/caseReducer";
import type { ProviderSet } from "../types";

export type HowTab = "steps" | "proof" | "decision";

const STEP_ICONS: Record<StepIcon, typeof Check> = { spark: Sparkles, search: Search, code: Code, flag: Flag, shield: Shield, coins: Coins, users: Users };

const Dots = () => (
  <span className="typing" aria-hidden>
    <i />
    <i />
    <i />
  </span>
);

function Steps({ rows }: { rows: StepRow[] }) {
  return (
    <>
      {rows.map((r) => {
        const Icon = STEP_ICONS[r.icon];
        const cls = r.state === "done" ? "" : r.state;
        return (
          <div key={r.node}>
            {r.phase && <div className="sec">{r.phase}</div>}
            <div className={`st ${cls}`} aria-current={r.state === "now" ? "step" : undefined}>
              <span className="n">{r.n}</span>
              <span className={`ic ${r.kind}`}>
                <Icon className="ico" aria-hidden />
              </span>
              <b>{r.name}</b>
              <span className="r" title={r.result}>
                {r.result}
              </span>
              <span className="t">{r.state === "now" ? r.kind === "ai" ? <Dots /> : <LoaderCircle className="ico spin" aria-hidden /> : r.time}</span>
            </div>
          </div>
        );
      })}
      <div className="legend">
        <span>
          <span className="ic ai">
            <Sparkles className="ico" aria-hidden />
          </span>
          AI · can't move money
        </span>
        <span>
          <span className="ic code">
            <Code className="ico" aria-hidden />
          </span>
          Plain code
        </span>
      </div>
    </>
  );
}

function ClaimMark({ status }: { status: ClaimRow["status"] }) {
  switch (status) {
    case "ok":
      return (
        <span className="chk" title="True">
          <Check className="ico" aria-hidden />
        </span>
      );
    case "bad":
      return (
        <span className="chk bad" title="Not true">
          <X className="ico" aria-hidden />
        </span>
      );
    case "part":
      return (
        <span className="chk part" title="Partly true">
          ◐
        </span>
      );
    default:
      return (
        <span className="chk no" title="Can't be checked from the records">
          ?
        </span>
      );
  }
}

function Proof({ view }: { view: CaseView }) {
  const trail = deriveTrail(view);
  const claims = deriveClaims(view);
  const cause = deriveCause(view);
  return (
    <>
      <div className="sec">Money trail</div>
      {trail.length ? (
        <div className="tl">
          {trail.map((r) => (
            <div key={r.key} className={`ev ${r.row}`} title={r.title}>
              <span className="d">{r.date}</span>
              <span className="lb">{r.row === "key" ? <b>{r.label}</b> : r.label}</span>
              <span className="fl">
                {r.chip && <span className="chip">{r.chip}</span>}
                {r.flags.map((f) => (
                  <span key={f.text} className={`flag ${f.cls}`}>
                    {f.text}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">{view.trail ? "Nothing on the money trail." : "Not followed yet."}</p>
      )}

      <div className="sec">What the merchant said</div>
      {claims.length ? (
        claims.map((c, i) => (
          <div className="cl" key={i}>
            <ClaimMark status={c.status} />
            <div>
              <div className="what" title={c.title}>
                {c.said}
              </div>
              <div className="why" title={c.finding}>
                {c.finding}
              </div>
            </div>
          </div>
        ))
      ) : (
        <p className="muted">{view.statements ? "No specific claims to check." : "Not checked yet."}</p>
      )}

      <div className="sec">Cause</div>
      {cause ? (
        <>
          <div className={`cause ${cause.box}`}>
            <span className={`flag ${cause.flag.cls}`}>{cause.flag.text}</span>
            <b>{cause.title}</b>
          </div>
          {cause.headline && <p className="headline">{cause.headline}</p>}
          {cause.ruledOut.length > 0 && (
            <div className="out">
              Ruled out:
              {cause.ruledOut.map((r, i) => (
                <span key={r}>
                  <s>{r}</s>
                  {i < cause.ruledOut.length - 1 ? " ·" : ""}
                </span>
              ))}
            </div>
          )}
        </>
      ) : (
        <p className="muted">No verdict yet.</p>
      )}

      {view.probes.length > 0 && (
        <>
          <div className="sec">Its own SQL (read-only)</div>
          {view.probes.map((pr) => (
            <div className="probe" key={pr.step}>
              <span className={`chk${pr.ok ? "" : " bad"}`}>{pr.ok ? <Check className="ico" aria-hidden /> : <X className="ico" aria-hidden />}</span>
              <div>
                {pr.purpose}
                <code>{pr.sql.trim()}</code>
              </div>
              <span className="cnt">{pr.ok ? `${pr.rows?.length ?? 0} row${pr.rows?.length === 1 ? "" : "s"}` : "error"}</span>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function CheckMark({ status }: { status: string }) {
  switch (status) {
    case "PASS":
      return <Check className="ico" aria-hidden />;
    case "FIXED":
    case "WARN":
      return <>!</>;
    case "FAIL":
      return <X className="ico" aria-hidden />;
    default:
      return <>–</>;
  }
}
const CHECK_CLASS: Record<string, string> = { PASS: "chk", FIXED: "chk fix", WARN: "chk warn", FAIL: "chk bad", SKIP: "chk skip" };

const RAN_MARK: Record<RanLine["tone"], { cls: string; icon: ReactNode }> = {
  ok: { cls: "chk", icon: <Check className="ico" aria-hidden /> },
  warn: { cls: "chk warn", icon: <Flag className="ico" aria-hidden /> },
  hum: { cls: "chk hum", icon: <Hourglass className="ico" aria-hidden /> },
  bad: { cls: "chk bad", icon: <Ban className="ico" aria-hidden /> },
};

function Decision({ view }: { view: CaseView }) {
  const d = deriveDecision(view);
  if (!d) return <p className="muted">{view.proposal ? "The safety rules haven't run yet." : "No fix proposed yet."}</p>;
  const endMark =
    d.end.cls === "ok" ? (
      <span className="chk">
        <Check className="ico" aria-hidden />
      </span>
    ) : d.end.cls === "hum" ? (
      <span className="chk hum">
        <Hourglass className="ico" aria-hidden />
      </span>
    ) : d.end.cls === "bad" ? (
      <span className="chk bad">
        <Ban className="ico" aria-hidden />
      </span>
    ) : (
      <span className="chk skip">…</span>
    );
  return (
    <>
      <div className="sec">The fix</div>
      <div className="flow">
        <div className={`fbox ai${d.proposed.struck ? " struck" : ""}`}>
          <span className="ic ai">
            <Sparkles className="ico" aria-hidden />
          </span>
          <small>AI proposed</small>
          <b>{d.proposed.value}</b>
        </div>
        <ArrowRight className="ico" aria-hidden />
        <div className={`fbox ${d.rules.cls}`}>
          <span className="ic code">
            <Shield className="ico" aria-hidden />
          </span>
          <small>{d.rules.small}</small>
          <b>{d.rules.value}</b>
        </div>
        <ArrowRight className="ico" aria-hidden />
        <div className={`fbox ${d.end.cls}`}>
          {endMark}
          <small>{d.end.small}</small>
          <b>{d.end.value}</b>
        </div>
      </div>
      <p className="whyline">{d.why}</p>

      <div className="sec">{d.checks.length} safety checks</div>
      <div className="checks">
        {d.checks.map((c, i) => (
          <span key={i} className={CHECK_CLASS[c.status] ?? "chk skip"} title={`${c.label}: ${c.detail}`}>
            <CheckMark status={c.status} />
          </span>
        ))}
      </div>
      {d.notes.map((n, i) => (
        <div className="fixnote" key={i}>
          <span className={CHECK_CLASS[n.status] ?? "chk skip"}>
            <CheckMark status={n.status} />
          </span>
          <span>
            <b>{n.label}:</b> <span className="d">{n.detail}</span>
          </span>
        </div>
      ))}

      <div className="sec">What ran</div>
      {d.ran.length ? (
        d.ran.map((r, i) => (
          <div className="did" key={i} title={r.detail}>
            <span className={RAN_MARK[r.tone].cls}>{RAN_MARK[r.tone].icon}</span>
            {r.text}
            <span className="spacer" />
            <span className="chips">
              {r.chips.map((c) => (
                <span className="chip" key={c}>
                  {c}
                </span>
              ))}
            </span>
          </div>
        ))
      ) : (
        <p className="muted">Nothing ran yet.</p>
      )}
      {d.others.length > 0 && <div className="other">Other actions checked: {d.others.join(" · ")}</div>}
    </>
  );
}

interface Props {
  view: CaseView;
  providers: ProviderSet | null;
  mode: "working" | "how";
  tab: HowTab;
  onTab: (t: HowTab) => void;
  onClose: () => void;
}

const TABS: [HowTab, string][] = [
  ["steps", "Steps"],
  ["proof", "Proof"],
  ["decision", "Decision"],
];

export function WorkPanel({ view, providers, mode, tab, onTab, onClose }: Props) {
  const steps = deriveSteps(view, providers);
  if (mode === "working") {
    return (
      <aside className="panel" aria-label="Munim is working">
        <div className="phd">
          <span className="ic ai">
            <Sparkles className="ico" aria-hidden />
          </span>
          <h2>Munim is working</h2>
          <span className="spacer" />
          <span className="pill run">
            Step {steps.current} of {STEP_COUNT}
          </span>
          <button type="button" className="x" onClick={onClose} aria-label="Hide" title="Hide (the steps stay in How Munim decided)">
            <X className="ico" aria-hidden />
          </button>
        </div>
        <div className="prog" role="progressbar" aria-valuemin={0} aria-valuemax={STEP_COUNT} aria-valuenow={Math.round(steps.progress * STEP_COUNT)}>
          <i style={{ width: `${Math.round(steps.progress * 100)}%` }} />
        </div>
        <div className="pbody scroll">
          <Steps rows={steps.rows} />
        </div>
      </aside>
    );
  }
  return (
    <aside className="panel" aria-label="How Munim decided">
      <div className="phd">
        <h2>How Munim decided</h2>
        <button type="button" className="x" onClick={onClose} aria-label="Close" title="Close">
          <X className="ico" aria-hidden />
        </button>
      </div>
      <nav className="ptabs" role="tablist">
        {TABS.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "on" : ""} onClick={() => onTab(id)}>
            {label}
          </button>
        ))}
      </nav>
      <div className="pbody scroll" role="tabpanel">
        {tab === "steps" ? <Steps rows={steps.rows} /> : tab === "proof" ? <Proof view={view} /> : <Decision view={view} />}
      </div>
    </aside>
  );
}
