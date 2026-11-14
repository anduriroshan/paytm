// The conversation, for the support teammate. Munim reports and answers in English. The merchant's language shows only
// where the merchant used it: their words (with an English reading under them) and replies going to them (on demand).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowRight, Ban, Check, CircleCheck, Copy, Flag, Globe, Hourglass, MessageSquarePlus, Pencil, Send, Shield, TriangleExclamationPoint, Users } from "lucide-react";
import { fmtMs, langLabel, rupees } from "../format";
import { clock, firstName, isEnglish, langTag, resultCard, type ResultCard, type ResultLine } from "../lib/explain";
import type { CaseView, ChatMessage } from "../state/caseReducer";
import { Composer } from "./Composer";
import { SpeakButton } from "./voice";

export interface FollowChip {
  key: string;
  label: string;
  language: string | null;
  onClick: () => void;
}

export interface SweepLine {
  count: number;
  /** "merchant" or "passenger". */
  who: string;
  total: number;
  fixed: { executed: number; total: number; simulated: boolean; merchants: number; all: boolean } | null;
}

interface Props {
  view: CaseView;
  starting: string | null;
  pendingQ: string | null;
  typingText: string | null;
  draft: string;
  onDraft: (v: string) => void;
  onSend: (text: string) => void;
  composer: { placeholder: string; disabled: boolean; busy: boolean; note: string | null; noteBad: boolean };
  chips: FollowChip[];
  voice: boolean;
  sweep: SweepLine | null;
  onOpenHow: () => void;
  onReview: () => void;
  onNewChat: () => void;
  /** A teammate sends Munim's draft (optionally edited). Nothing reaches the merchant before this. */
  onSendReply: (m: ChatMessage, text?: string) => Promise<void>;
}

const Dots = () => (
  <span className="typing" aria-hidden>
    <i />
    <i />
    <i />
  </span>
);

const hasIndic = (s: string) => /[ऀ-෿]/.test(s);

/** Right-hand bubble: the merchant's complaint, or the teammate's question (which may be the merchant's words pasted in). */
function MerchantBubble({ text, language, en, at, voiceNote, waitEn, from }: { text: string; language: string | null; en: string | null; at: string | null; voiceNote: boolean; waitEn: boolean; from?: string }) {
  const english = isEnglish(language) && !hasIndic(text);
  const tm = [from ?? "", clock(at), voiceNote ? "voice note" : ""].filter(Boolean).join(" · ");
  return (
    <div className="row me">
      <div className="bub">
        {english ? (
          <div>{text}</div>
        ) : (
          <div className={`orig${hasIndic(text) ? " ind" : ""}`}>
            {language && <span className="lang">{langLabel(language).toUpperCase()}</span>}
            {text}
          </div>
        )}
        {!english &&
          (en ? (
            <div className="tr">
              <Globe className="ico" aria-hidden />
              <span>{en}</span>
            </div>
          ) : waitEn ? (
            <div className="tr wait">
              <Globe className="ico" aria-hidden />
              <span>Translating…</span>
            </div>
          ) : null)}
        {tm && (
          <div className="tm">
            <span>{tm}</span>
          </div>
        )}
      </div>
    </div>
  );
}

const LINE_ICON: Record<ResultLine["tone"], { cls: string; Icon: typeof Check }> = {
  ok: { cls: "chk", Icon: Check },
  warn: { cls: "chk warn", Icon: Flag },
  hum: { cls: "chk hum", Icon: Hourglass },
  bad: { cls: "chk bad", Icon: Ban },
};

function ResultBox({ card, onOpenHow }: { card: ResultCard; onOpenHow: () => void }) {
  return (
    <div className={`result ${card.tint}`}>
      {card.lines.map((l, i) => {
        const { cls, Icon } = LINE_ICON[l.tone];
        return (
          <div className="ln" key={i}>
            <span className={cls}>
              <Icon className="ico" aria-hidden />
            </span>
            {l.text}
          </div>
        );
      })}
      {card.fix && (
        <div className="fixline">
          <Shield className="ico" aria-hidden />
          <span>
            Safety rules fixed the AI's amount: <s>{card.fix.from}</s> → <b>{card.fix.to}</b>
          </span>
        </div>
      )}
      <button type="button" className="link go" onClick={onOpenHow}>
        How Munim decided <ArrowRight className="ico" aria-hidden />
      </button>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      /* clipboard blocked: nothing to do */
    }
  };
  return (
    <button type="button" className="btn" onClick={onClick} title="Copy the reply">
      {done ? <Check className="ico" aria-hidden /> : <Copy className="ico" aria-hidden />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

/** What goes to the merchant, folded away: the team reads Munim's English above; this opens on demand. */
function ReplyBlock({ title, text, en, language, voice, copy }: { title: string; text: string; en: string | null; language: string | null; voice: boolean; copy: boolean }) {
  const [open, setOpen] = useState(false);
  const native = !isEnglish(language) || hasIndic(text);
  return (
    <div className="sent">
      <div className="sh">
        <Send className="ico" aria-hidden />
        <span className="t">{title}</span>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? "Hide" : "Read"}
        </button>
        {copy && <CopyButton text={text} />}
        {voice && <SpeakButton text={text} language={language} />}
      </div>
      {open && (
        <div className="sb">
          {native && en && <div>{en}</div>}
          <div className={native ? "native ind" : ""}>{text}</div>
        </div>
      )}
    </div>
  );
}

/** Munim's reply waiting on the case: the team reads it, edits if needed, and presses Send. */
function DraftBlock({ m, kind, to, voice, onSend }: { m: ChatMessage; kind: MunimKind; to: string; voice: boolean; onSend: (text?: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.text);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const native = !isEnglish(m.language) || hasIndic(m.text);
  const title = `${kind === "update" ? "Update for" : "Reply to"} ${to}${native ? ` · ${langLabel(m.language)}` : ""}`;
  const changed = editing && text.trim() !== m.text.trim();
  const send = async () => {
    if (busy || (editing && !text.trim())) return;
    setBusy(true);
    setErr(null);
    try {
      await onSend(changed ? text.trim() : undefined);
      setEditing(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not send");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="draftbox">
      <div className="dh">
        <Send className="ico" aria-hidden />
        <span className="t">{title}</span>
        <span className="pill warn">Not sent</span>
      </div>
      <div className="db">
        {native && m.en && <div className="en">{m.en}</div>}
        {editing ? (
          <textarea className={native ? "ind" : ""} value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(8, Math.max(3, Math.ceil(text.length / 70)))} maxLength={2000} aria-label={`Edit the reply to ${to}`} autoFocus />
        ) : (
          <div className={native ? "native ind" : "native"}>{m.text}</div>
        )}
      </div>
      <div className="df">
        <button type="button" className="btn pri" onClick={send} disabled={busy || (editing && !text.trim())}>
          <Send className="ico" aria-hidden />
          {busy ? "Sending…" : changed ? "Send edited" : `Send to ${to}`}
        </button>
        {editing ? (
          <button type="button" className="btn" onClick={() => { setEditing(false); setText(m.text); }} disabled={busy}>
            Cancel
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => setEditing(true)} disabled={busy}>
            <Pencil className="ico" aria-hidden />
            Edit
          </button>
        )}
        {voice && <SpeakButton text={editing ? text : m.text} language={m.language} />}
        <span className="note">{err ?? `Nothing goes to ${to} until you press Send.`}</span>
      </div>
    </div>
  );
}

type MunimKind = "main" | "update" | "chat";

function MunimBubble({ m, kind, name, lead, extra, took, voice, onNewChat, onSendReply }: { m: ChatMessage; kind: MunimKind; name: string; lead: string | null; extra: ReactNode; took: string; voice: boolean; onNewChat: () => void; onSendReply: (m: ChatMessage, text?: string) => Promise<void> }) {
  const lang = m.language;
  const native = !isEnglish(lang) || hasIndic(m.text);
  const label = langLabel(lang);
  const to = name || "the merchant";

  if (m.streaming || !m.final) {
    return (
      <div className="row mu">
        <span className="mav">M</span>
        <div className="bub">
          {lead && <div className="lead">{lead}</div>}
          <div className="writing">
            <Dots />
            Writing the reply to {to}{native ? ` in ${label}` : ""}…
          </div>
          <div className={`streamtext${hasIndic(m.text) ? " ind" : ""}`}>{m.text}</div>
        </div>
      </div>
    );
  }

  const tm = [clock(m.at), took].filter(Boolean).join(" · ");
  const inLang = native ? ` · ${label}` : "";
  // Chat answers are English for the teammate, with an optional draft for the merchant.
  // The case reply and approval updates went to the merchant: Munim's English lead on top, the message itself folded.
  const body = kind === "chat" ? m.text : lead;
  const reply =
    kind === "chat"
      ? m.reply
        ? { title: `Reply for ${to}${isEnglish(m.reply.language) ? "" : ` · ${langLabel(m.reply.language)}`}`, text: m.reply.text, en: null, language: m.reply.language, copy: true }
        : null
      : m.draft
        ? null
        : { title: `${kind === "update" ? "Update sent" : "Sent"} to ${to}${inLang}${m.sent ? ` · by ${m.sent.by}${m.sent.edited ? " · edited" : ""}` : ""}`, text: m.text, en: m.en ?? null, language: lang, copy: false };
  return (
    <div className="row mu">
      <span className="mav">M</span>
      <div className="bub">
        {body && <div className="lead">{body}</div>}
        {extra}
        {m.sources && m.sources.length > 0 && (
          <div className="src">
            {m.sources.map((s) => (
              <span className="chip" key={s}>
                {s}
              </span>
            ))}
          </div>
        )}
        {m.newCase && (
          <div className="src">
            <button type="button" className="btn" onClick={onNewChat}>
              <MessageSquarePlus className="ico" aria-hidden />
              Start a new chat
            </button>
          </div>
        )}
        {kind !== "chat" && m.draft && <DraftBlock m={m} kind={kind} to={to} voice={voice} onSend={(t) => onSendReply(m, t)} />}
        {reply && <ReplyBlock {...reply} voice={voice} />}
        {tm && (
          <div className="tm">
            <span>{tm}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export function ChatView(p: Props) {
  const { view } = p;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const msgs = view.messages;
  const mainIdx = msgs.findIndex((m) => m.role === "munim" && m.kind !== "chat");
  const complaintIdx = msgs.findIndex((m) => m.role === "merchant");
  const name = firstName(view.info?.merchant?.owner_name);
  const anyOpenReply = msgs.some((m) => m.role === "munim" && !m.final);
  const card = view.done ? resultCard(view) : null;
  const last = msgs[msgs.length - 1];
  const thinking = !!p.pendingQ || last?.role === "team";

  // Keep the newest message in view: while the reader is at the bottom, stay pinned there as anything grows.
  const msgsRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  useEffect(() => {
    const el = scrollRef.current;
    const inner = msgsRef.current;
    if (!el || !inner) return;
    const pin = () => {
      if (stickRef.current) el.scrollTop = el.scrollHeight;
    };
    const ro = new ResizeObserver(pin);
    ro.observe(inner);
    ro.observe(el);
    pin();
    return () => ro.disconnect();
  }, []);
  const tail = `${msgs.length}|${last?.text.length ?? 0}|${p.typingText ?? ""}|${p.pendingQ ?? ""}|${view.done ? 1 : 0}|${p.sweep?.fixed ? 1 : 0}|${view.errors.length}`;
  useEffect(() => {
    const el = scrollRef.current;
    if (p.pendingQ) stickRef.current = true; // asking a question always brings the answer into view
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [tail, p.pendingQ]);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };

  const sweepLine = p.sweep && p.sweep.count > 0 && (
    <div className={`sys${p.sweep.fixed?.all ? " ok" : ""}`} key="sweep">
      {p.sweep.fixed?.all ? <CircleCheck className="ico" aria-hidden /> : <Users className="ico" aria-hidden />}
      {p.sweep.fixed && !p.sweep.fixed.all ? (
        <>
          <span>
            Fixed for {p.sweep.fixed.merchants} of {p.sweep.count} {p.sweep.who}s · {rupees(p.sweep.fixed.total)} refunded{p.sweep.fixed.simulated ? " (simulated)" : ""}
          </span>
          <button type="button" className="link" onClick={p.onReview}>
            Review the rest
          </button>
        </>
      ) : p.sweep.fixed ? (
        <span>
          Fixed for {p.sweep.fixed.executed ? `${p.sweep.count} more ${p.sweep.who}s` : `the other ${p.sweep.who}s`} · {rupees(p.sweep.fixed.total)} refunded
          {p.sweep.fixed.simulated ? " (simulated)" : ""}
        </span>
      ) : (
        <>
          <span>
            Same bug found in {p.sweep.count} more {p.sweep.who}s · {rupees(p.sweep.total)}
          </span>
          <button type="button" className="link" onClick={p.onReview}>
            Review
          </button>
        </>
      )}
    </div>
  );

  return (
    <section className="chat" aria-label="Chat">
      <div className="chatscroll scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="msgs" ref={msgsRef}>
          {p.starting && !view.info && <MerchantBubble text={p.starting} language={null} en={null} at={null} voiceNote={false} waitEn={false} />}
          {msgs.map((m, i) => {
            if (m.role !== "munim") {
              const isComplaint = i === complaintIdx;
              const en = isComplaint ? (m.en ?? view.intake?.text_en ?? null) : (m.en ?? null);
              return (
                <MerchantBubble
                  key={m.id}
                  text={m.text}
                  language={isComplaint ? (view.intake?.language ?? m.language) : m.language}
                  en={en}
                  at={m.at}
                  voiceNote={isComplaint && view.info?.channel === "voice"}
                  waitEn={isComplaint && !view.intake}
                  from={isComplaint && name ? `From ${name}${view.info?.channel === "app" ? " · merchant app" : ""}` : undefined}
                />
              );
            }
            const isMain = i === mainIdx;
            const kind: MunimKind = m.kind === "chat" ? "chat" : isMain ? "main" : "update";
            const extra = isMain && card ? <ResultBox card={card} onOpenHow={p.onOpenHow} /> : null;
            const took = isMain && view.done?.ms ? `took ${fmtMs(view.done.ms)}` : "";
            // Munim's report to the teammate: the verdict in plain English (written by code, not the model).
            const lead = isMain ? view.verdict?.headline?.trim() || null : null;
            return [
              <MunimBubble key={m.id} m={m} kind={kind} name={name} lead={lead} extra={extra} took={took} voice={p.voice} onNewChat={p.onNewChat} onSendReply={p.onSendReply} />,
              isMain && m.final ? sweepLine : null,
            ];
          })}
          {mainIdx < 0 && view.done && sweepLine}
          {view.errors.map((e, i) => (
            <div className="sys bad" key={`err-${i}`} role="alert">
              <TriangleExclamationPoint className="ico" aria-hidden />
              {e.message}
            </div>
          ))}
          {p.pendingQ && <MerchantBubble text={p.pendingQ} language={null} en={null} at={null} voiceNote={false} waitEn={false} />}
          {((p.typingText && !anyOpenReply) || thinking) && (
            <div className="row mu">
              <span className="mav">M</span>
              <div className="bub">
                <Dots />
                {thinking ? "Looking at the case file…" : p.typingText}
              </div>
            </div>
          )}
          {view.done && mainIdx < 0 && card && (card.lines.length > 0 || card.fix) && (
            <div className="row mu">
              <span className="mav">M</span>
              <div className="bub">
                <ResultBox card={card} onOpenHow={p.onOpenHow} />
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="composer">
        {p.chips.length > 0 && (
          <div className="follow">
            {p.chips.map((c) => (
              <button key={c.key} type="button" onClick={c.onClick}>
                {c.language && <i className="lang">{langTag(c.language)}</i>}
                {c.label}
              </button>
            ))}
          </div>
        )}
        <Composer value={p.draft} onChange={p.onDraft} onSend={(t) => p.onSend(t)} placeholder={p.composer.placeholder} disabled={p.composer.disabled} busy={p.composer.busy} voice={p.voice} />
        {p.composer.note && <div className={`cnote${p.composer.noteBad ? " bad" : ""}`}>{p.composer.note}</div>}
      </div>
    </section>
  );
}
