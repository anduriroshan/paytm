// Inbox: every complaint that reached support (call, email, ticket, merchant app), by source and status.
// A case row opens the chat with Munim's work; a new row shows the complaint as it came, with "Continue with Munim".
import { useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, Check, Clock, LoaderCircle, Mail, MessageSquare, Phone, Smartphone, Sparkles, Ticket } from "lucide-react";
import { langLabel } from "../format";
import { firstName, initials } from "../lib/explain";
import type { InboxItem } from "../types";
import "./inbox.css";

type StatusTone = "new" | "run" | "draft" | "hum" | "sent" | "esc" | "bad" | "gray";
export interface InboxStatus {
  key: string;
  label: string;
  tone: StatusTone;
}

/** One status per row, most urgent first: what a teammate has to do next. */
export function inboxStatus(i: InboxItem): InboxStatus {
  if (i.status === "NEW") return { key: "new", label: "New", tone: "new" };
  if (i.status === "INVESTIGATING") return { key: "run", label: "Munim working", tone: "run" };
  if (i.approvals > 0) return { key: "approval", label: "Needs approval", tone: "hum" };
  if (i.reply === "draft") return { key: "draft", label: "Reply ready", tone: "draft" };
  if (i.status === "BLOCKED") return { key: "blocked", label: "Blocked", tone: "bad" };
  if (i.status === "ESCALATED") return { key: "team", label: "With a team", tone: "esc" };
  if (i.reply === "sent") return { key: "sent", label: "Sent", tone: "sent" };
  if (i.status === "RESOLVED") return { key: "done", label: "Resolved", tone: "sent" };
  return { key: "other", label: i.status.charAt(0) + i.status.slice(1).toLowerCase().replace(/_/g, " "), tone: "gray" };
}

const SOURCES: Record<string, { label: string; Icon: typeof Phone; cls: string }> = {
  call: { label: "Call", Icon: Phone, cls: "call" },
  email: { label: "Email", Icon: Mail, cls: "mail" },
  ticket: { label: "Ticket", Icon: Ticket, cls: "tkt" },
  app: { label: "Merchant app", Icon: Smartphone, cls: "app" },
  chat: { label: "Chat", Icon: MessageSquare, cls: "chat" },
};
const sourceOf = (via: string) => SOURCES[via] ?? SOURCES.chat;
const PLURAL: Record<string, string> = { call: "Calls", email: "Emails", ticket: "Tickets", app: "App", chat: "Chats" };

const shortLang = (code: string | null | undefined) => (!code ? "—" : code === "hi-Latn" ? "HINGLISH" : code.toUpperCase());
const hasIndic = (s: string) => /[ऀ-෿]/.test(s);

function Source({ via, note }: { via: string; note?: string }) {
  const s = sourceOf(via);
  return (
    <span className="isrc">
      <span className={`ic ${s.cls}`}>
        <s.Icon className="ico" aria-hidden />
      </span>
      <span>
        {s.label}
        {note && <small>{note}</small>}
      </span>
    </span>
  );
}

interface Props {
  items: InboxItem[];
  loading: boolean;
  /** offline: the rows are the recorded demo cases, and Continue plays the recording */
  offline: boolean;
  activeCaseId: string | null;
  busy: boolean;
  error: string | null;
  onOpenCase: (caseId: string) => void;
  onContinue: (item: InboxItem) => void;
}

export function Inbox({ items, loading, offline, activeCaseId, busy, error, onOpenCase, onContinue }: Props) {
  const [via, setVia] = useState<string | null>(null);
  const [st, setSt] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  const rows = useMemo(() => items.map((i) => ({ i, s: inboxStatus(i) })), [items]);
  const vias = useMemo(() => [...new Set(rows.map((r) => r.i.via))], [rows]);
  const count = (f: (r: (typeof rows)[number]) => boolean) => rows.filter(f).length;
  const statusChips = [
    { key: "new", label: "New" },
    { key: "draft", label: "Reply ready" },
    { key: "approval", label: "Needs approval" },
  ].filter((c) => count((r) => r.s.key === c.key) > 0);
  const shown = rows.filter((r) => (!via || r.i.via === via) && (!st || r.s.key === st));

  const open = openId ? items.find((i) => i.id === openId) : undefined;
  if (open) {
    const s = sourceOf(open.via);
    const name = firstName(open.owner_name) || open.business_name || "the merchant";
    return (
      <div className="needswrap scroll">
        <div className="inbox">
          <button type="button" className="link back" onClick={() => setOpenId(null)}>
            <ArrowLeft className="ico" aria-hidden />
            Inbox
          </button>
          <div className="ihead">
            <span className="av">{initials(open.business_name)}</span>
            <div>
              <b>{open.business_name ?? open.merchant_id}</b>
              <small>{[open.owner_name, open.city, open.language ? `speaks ${langLabel(open.language)}` : ""].filter(Boolean).join(" · ")}</small>
            </div>
            <span className="spacer" />
            <span className="ist new">New</span>
          </div>
          <div className="icase">
            <div className="icard">
              <header>
                <span className={`ic ${s.cls}`}>
                  <s.Icon className="ico" aria-hidden />
                </span>
                <h3>{open.via === "call" ? "Call transcript" : open.via === "email" ? "Email" : open.via === "ticket" ? "Ticket" : "Message"}</h3>
              </header>
              <div className="imeta">
                <span>
                  <Clock className="ico" aria-hidden />
                  {[open.received, open.via_note].filter(Boolean).join(" · ") || "Today"}
                </span>
                <span>{open.summary}</span>
              </div>
              <div className="isaid">
                <span className="spk">{name}</span>
                <div className={`say${hasIndic(open.text) ? " ind" : ""}`}>
                  <span className="lang">{shortLang(open.language)}</span>
                  {open.text}
                </div>
              </div>
            </div>
            <div className="icard hand">
              <div className="mm">M</div>
              <h2>Hand this to Munim</h2>
              <p>Munim checks what {name} said against the money trail, fixes what the rules allow and drafts a reply.</p>
              <button type="button" className="go" onClick={() => onContinue(open)} disabled={busy}>
                {busy ? <LoaderCircle className="ico spin" aria-hidden /> : <Sparkles className="ico" aria-hidden />}
                {offline ? "Play the recording" : "Continue with Munim"}
                <ArrowRight className="ico" aria-hidden />
              </button>
              {error && <div className="ierr">{error}</div>}
              <ul className="ticks">
                <li>
                  <Check className="ico" aria-hidden />
                  Nothing is sent to {name} without you
                </li>
                <li>
                  <Check className="ico" aria-hidden />
                  Big refunds wait for a person's approval
                </li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="needswrap scroll">
      <div className="inbox">
        <h1>Inbox</h1>
        <p>Every complaint, by source and status.{offline ? " Offline: these play the recorded demo cases." : ""}</p>
        <div className="ifilters">
          <button type="button" className={`ichip${!via && !st ? " on" : ""}`} onClick={() => { setVia(null); setSt(null); }}>
            All <i>{rows.length}</i>
          </button>
          {vias.map((v) => {
            const s = sourceOf(v);
            return (
              <button type="button" key={v} className={`ichip${via === v ? " on" : ""}`} onClick={() => setVia(via === v ? null : v)}>
                <s.Icon className="ico" aria-hidden />
                {PLURAL[v] ?? s.label} <i>{count((r) => r.i.via === v)}</i>
              </button>
            );
          })}
          {statusChips.length > 0 && <span className="sep" />}
          {statusChips.map((c) => (
            <button type="button" key={c.key} className={`ichip${st === c.key ? " on" : ""}`} onClick={() => setSt(st === c.key ? null : c.key)}>
              {c.label} <i>{count((r) => r.s.key === c.key)}</i>
            </button>
          ))}
        </div>
        {loading && items.length === 0 ? (
          <div className="empty">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="empty">Nothing here.</div>
        ) : (
          <table className="itbl">
            <thead>
              <tr>
                <th style={{ width: 150 }}>Source</th>
                <th>Customer</th>
                <th>What it's about</th>
                <th>Lang</th>
                <th>Received</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {shown.map(({ i, s }) => {
                const go = () => (i.case_id ? onOpenCase(i.case_id) : setOpenId(i.id));
                return (
                  <tr
                    key={i.id}
                    className={i.case_id && i.case_id === activeCaseId ? "on" : ""}
                    tabIndex={0}
                    onClick={go}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        go();
                      }
                    }}
                    aria-label={`${i.business_name ?? i.merchant_id}: ${s.label}`}
                  >
                    <td>
                      <Source via={i.via} note={i.via_note} />
                    </td>
                    <td className="mer">
                      <b>{i.business_name ?? i.merchant_id}</b>
                      <span>{i.city}</span>
                    </td>
                    <td className="said">{i.summary}</td>
                    <td>
                      <span className="lang">{shortLang(i.language)}</span>
                    </td>
                    <td className="time">{i.received || "—"}</td>
                    <td>
                      <span className={`ist ${s.tone}`}>
                        {s.key === "sent" && <Check className="ico" aria-hidden />}
                        {s.key === "run" && <LoaderCircle className="ico spin" aria-hidden />}
                        {s.label}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
