// What Munim stopped and handed to a human: fixes over the auto-limit, the fleet-wide batch fix, and cases sent to other teams.
import { Ban, Check, CircleCheck, LoaderCircle, Shield, TriangleExclamationPoint, Users } from "lucide-react";
import { rupees } from "../format";
import { cap, initials } from "../lib/explain";

export interface CaseApproval {
  id: number;
  caseId: string | null;
  name: string;
  sub: string;
  amount: number;
  noun: string;
  reason: string;
}
export interface SweepGroup {
  sweepId: string;
  cause: string;
  merchants: number;
  total: number;
  actions: number;
  names: string[];
}
export interface HandedCase {
  caseId: string;
  name: string;
  sub: string;
  pill: string;
  reason: string | null;
}
export interface Decided {
  key: string;
  text: string;
  bad: boolean;
}

interface Props {
  approvals: CaseApproval[];
  sweeps: SweepGroup[];
  handed: HandedCase[];
  decided: Decided[];
  busy: string | null;
  replay: boolean;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onApproveSweep: (sweepId: string) => void;
  onOpen: ((caseId: string) => void) | null;
}

const NAMES_SHOWN = 3;

export function NeedsYou({ approvals, sweeps, handed, decided, busy, replay, onApprove, onReject, onApproveSweep, onOpen }: Props) {
  const nothing = approvals.length === 0 && sweeps.length === 0;
  const liveOnly = replay ? "Needs the live backend" : undefined;
  return (
    <div className="needswrap scroll">
      <div className="needs">
        <h1>Needs you</h1>
        <p>Munim stops and asks when a fix is too big or not its call.</p>

        {decided.map((d) => (
          <div key={d.key} className={`ok-line${d.bad ? " bad" : ""}`}>
            {d.bad ? <Ban className="ico" aria-hidden /> : <CircleCheck className="ico" aria-hidden />}
            {d.text}
          </div>
        ))}

        {nothing && <div className="empty">Nothing needs you right now.</div>}

        {approvals.map((a) => {
          const isBusy = busy === `a${a.id}`;
          return (
            <div className="ncard" key={a.id}>
              <div className="nhead">
                <span className="av">{initials(a.name)}</span>
                <div>
                  <b>{a.name}</b>
                  <small>{a.sub}</small>
                </div>
                <span className="spacer" />
                {onOpen && a.caseId && (
                  <button type="button" className="link" onClick={() => onOpen(a.caseId as string)}>
                    Open chat
                  </button>
                )}
              </div>
              <div className="nbody">
                <span className="amt">
                  {a.amount ? (
                    <>
                      {rupees(a.amount)} <small>{a.noun}</small>
                    </>
                  ) : (
                    cap(a.noun)
                  )}
                </span>
                <span className="reason">
                  <TriangleExclamationPoint className="ico" aria-hidden />
                  {a.reason}
                </span>
                <span className="spacer" />
                <button type="button" className="btn" onClick={() => onReject(a.id)} disabled={replay || !!busy} title={liveOnly}>
                  Reject
                </button>
                <button type="button" className="btn ok" onClick={() => onApprove(a.id)} disabled={replay || !!busy} title={liveOnly}>
                  {isBusy ? <LoaderCircle className="ico spin" aria-hidden /> : <Check className="ico" aria-hidden />}
                  Approve
                </button>
              </div>
            </div>
          );
        })}

        {sweeps.map((s) => {
          const more = s.names.length - NAMES_SHOWN;
          const isBusy = busy === `s${s.sweepId}`;
          return (
            <div className="ncard" key={s.sweepId}>
              <div className="nhead">
                <span className="ic code">
                  <Users className="ico" aria-hidden />
                </span>
                <div>
                  <b>Same bug in {s.merchants} more merchants</b>
                  <small>{s.cause}</small>
                </div>
              </div>
              <div className="nbody">
                <span className="amt">
                  {rupees(s.total)} <small>· {s.actions} actions</small>
                </span>
                <span className="reason">
                  <Users className="ico" aria-hidden />
                  Batch fix
                </span>
                <span className="spacer" />
                <button type="button" className="btn ok" onClick={() => onApproveSweep(s.sweepId)} disabled={!!busy} title={replay ? "Simulated: the recording has no backend" : undefined}>
                  {isBusy ? <LoaderCircle className="ico spin" aria-hidden /> : <Check className="ico" aria-hidden />}
                  Approve all
                </button>
              </div>
              {s.names.length > 0 && (
                <div className="names">
                  {s.names.slice(0, NAMES_SHOWN).join(" · ")}
                  {more > 0 ? ` · +${more} more` : ""}
                </div>
              )}
            </div>
          );
        })}

        {handed.length > 0 && (
          <>
            <h3>Handed to other teams</h3>
            {handed.map((h) => (
              <div className="ncard dim" key={h.caseId}>
                <div className="nhead">
                  <span className="av">{initials(h.name)}</span>
                  <div>
                    <b>{h.name}</b>
                    <small>{h.sub}</small>
                  </div>
                  <span className="spacer" />
                  {onOpen && (
                    <button type="button" className="link" onClick={() => onOpen(h.caseId)}>
                      Open chat
                    </button>
                  )}
                </div>
                <div className="nbody">
                  {h.reason && (
                    <span className="reason gray">
                      <Shield className="ico" aria-hidden />
                      {h.reason}
                    </span>
                  )}
                  <span className="spacer" />
                  <span className="pill gray">{h.pill}</span>
                </div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
