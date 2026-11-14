// The batch fix, reviewed one merchant at a time: who, how much, and the rows that prove it. Only ticked merchants are fixed.
import { useState, type ReactNode } from "react";
import { Check, ChevronDown, ChevronRight, LoaderCircle } from "lucide-react";
import { rupees } from "../format";
import "./sweep.css";

export interface SweepReviewRow {
  id: string;
  name: string;
  city: string;
  amount: number;
  /** why Munim thinks the same bug hit this shop */
  proof: string[];
  /** the debits the fix pays back */
  ledger: { date: string; ref: string; amount: number; note: string }[];
}

interface Props {
  sweepId: string;
  merchants: number;
  /** "merchant" or "passenger". */
  who?: string;
  busy: boolean;
  disabled: boolean;
  simulated: boolean;
  preview: ReactNode;
  load: (sweepId: string) => Promise<{ rows: SweepReviewRow[]; note: string | null }>;
  onApprove: (merchantIds: string[]) => Promise<void>;
}

export function SweepReview({ sweepId, merchants, who = "merchant", busy, disabled, simulated, preview, load, onApprove }: Props) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<SweepReviewRow[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [proof, setProof] = useState<string | null>(null);

  const start = async () => {
    setOpen(true);
    setErr(null);
    setLoading(true);
    try {
      const r = await load(sweepId);
      setRows(r.rows);
      setNote(r.note);
      setPicked(new Set(r.rows.map((x) => x.id)));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  if (!open) {
    return (
      <>
        {preview}
        <div className="srv-go">
          <span className="spacer" />
          <button type="button" className="btn pri" onClick={start} disabled={disabled}>
            Review {merchants} {who}s
          </button>
        </div>
      </>
    );
  }

  const list = rows ?? [];
  const chosen = list.filter((r) => picked.has(r.id));
  const sum = chosen.reduce((s, r) => s + r.amount, 0);
  const all = list.length > 0 && chosen.length === list.length;
  const toggle = (id: string) =>
    setPicked((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const approve = async () => {
    if (!chosen.length) return;
    setErr(null);
    try {
      await onApprove(chosen.map((r) => r.id));
      setOpen(false);
      setRows(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="srv">
      <div className="srv-intro">Check each {who === "merchant" ? "shop" : who}. Only the ticked ones get the fix.</div>
      {loading ? (
        <div className="srv-load">
          <LoaderCircle className="ico spin" aria-hidden />
          Loading the {who}s…
        </div>
      ) : (
        <>
          <div className="srv-bar">
            <label className="srv-all">
              <input type="checkbox" checked={all} onChange={() => setPicked(all ? new Set() : new Set(list.map((r) => r.id)))} />
              {chosen.length} of {list.length} ticked
            </label>
            {note && <span className="srv-note">{note}</span>}
          </div>
          <div className="srv-list scroll">
            {list.map((r) => {
              const shown = proof === r.id;
              return (
                <div className={`srv-row${picked.has(r.id) ? "" : " off"}`} key={r.id}>
                  <div className="srv-main">
                    <input type="checkbox" checked={picked.has(r.id)} onChange={() => toggle(r.id)} aria-label={`Include ${r.name}`} />
                    <div className="srv-who">
                      <b>{r.name}</b>
                      <small>
                        {r.city} · {r.id}
                      </small>
                    </div>
                    <span className="srv-amt">{rupees(r.amount)}</span>
                    <button type="button" className="srv-proof" onClick={() => setProof(shown ? null : r.id)} aria-expanded={shown}>
                      Proof
                      {shown ? <ChevronDown className="ico" aria-hidden /> : <ChevronRight className="ico" aria-hidden />}
                    </button>
                  </div>
                  {shown && (
                    <div className="srv-detail">
                      {r.proof.length > 0 && (
                        <ul>
                          {r.proof.map((p, i) => (
                            <li key={i}>
                              <Check className="ico" aria-hidden />
                              {p}
                            </li>
                          ))}
                        </ul>
                      )}
                      {r.ledger.length > 0 && (
                        <table>
                          <thead>
                            <tr>
                              <th>Date</th>
                              <th>Debit</th>
                              <th>Amount</th>
                              <th>Note</th>
                            </tr>
                          </thead>
                          <tbody>
                            {r.ledger.map((l, i) => (
                              <tr key={i}>
                                <td>{l.date}</td>
                                <td className="mono">{l.ref}</td>
                                <td>{rupees(l.amount)}</td>
                                <td>{l.note}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
      {err && <div className="srv-err">{err}</div>}
      <div className="srv-foot">
        <button type="button" className="btn" onClick={() => setOpen(false)} disabled={busy}>
          Close
        </button>
        <span className="spacer" />
        {simulated && <span className="srv-note">Simulated: the recording has no backend</span>}
        <button type="button" className="btn ok" onClick={approve} disabled={busy || loading || chosen.length === 0}>
          {busy ? <LoaderCircle className="ico spin" aria-hidden /> : <Check className="ico" aria-hidden />}
          Approve {chosen.length} {chosen.length === 1 ? who : `${who}s`} · {rupees(sum)}
        </button>
      </div>
    </div>
  );
}
