// Plain-English view of a case, derived from the event-sourced CaseView. Pure functions, no React.
// Every short label the chat-first UI shows (steps, proof, decision, result card) is built here.
import { asArray, fmtMs, langLabel, rupees, timeKey } from "../format";
import { caseStatus, type CaseView, type ExecutionView } from "../state/caseReducer";
import type { ActionVerdict, CaseRow, EvidenceRef, FirewallCheck, ProposedAction, ProviderSet, StatementCheck, TrailNode } from "../types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DEMO_YEAR = "2026";

export const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
/** The firewall talks about the "LLM"; the team-facing UI says "AI". */
export const aiWords = (s: string) => s.replace(/\bLLM\b/g, "AI");

/** "2026-09-01…" → "1 Sep"; other years keep the year ("20 Nov 2025"); "2026-09" → "Sep". */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(iso);
  if (!m) return iso;
  const mon = MONTHS[Number(m[2]) - 1] ?? m[2];
  const base = m[3] ? `${Number(m[3])} ${mon}` : mon;
  return m[1] === DEMO_YEAR ? base : `${base} ${m[1]}`;
}

/** "10:42" from a naive ISO timestamp, read textually (no timezone shift). */
export function clock(iso: string | null | undefined): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso ?? "");
  return m ? `${m[1]}:${m[2]}` : "";
}

/** Short language tag for chips and bubbles. */
export function langTag(code: string | null | undefined): string {
  if (!code) return "";
  if (code === "hi-Latn") return "HINGLISH";
  return code.split("-")[0].toUpperCase();
}
export const isEnglish = (code: string | null | undefined) => !code || code.toLowerCase().startsWith("en");

/** First name for a greeting: skips initials ("K. Lakshmi" → "Lakshmi"). */
export function firstName(owner: string | null | undefined): string {
  const parts = (owner ?? "").split(/\s+/).filter(Boolean);
  return parts.find((p) => !/^[A-Za-z]\.?$/.test(p) && !p.endsWith(".")) ?? parts[0] ?? "";
}

export function initials(name: string | null | undefined): string {
  const parts = (name ?? "").split(/\s+/).filter((p) => /^[A-Za-z]/.test(p));
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "M";
}

// ───────────────────────────── causes ─────────────────────────────

export const CAUSE_TITLES: Record<string, string> = {
  ZOMBIE_BILLING: "Rent kept running after the return",
  DUPLICATE_SUBSCRIPTION: "Two rentals on one device",
  DUPLICATE_EDI: "Loan instalment taken twice",
  PICKUP_NOT_COMPLETED: "Device never collected",
  ALREADY_REFUNDED: "Already refunded",
  RISK_HOLD: "Held by the risk team",
  KYC_HOLD: "Held for KYC documents",
};
export const causeTitle = (id: string | null | undefined) => (id ? (CAUSE_TITLES[id] ?? cap(id.replace(/_/g, " ").toLowerCase())) : "");

// ───────────────────────────── actions ─────────────────────────────

/** Team named on an ESCALATE action (firewall final/proposed, or parsed from the receipt). */
export function escalationTeam(view: CaseView, ex?: ExecutionView | null): string {
  const fw = asArray(view.firewall?.actions).map((a) => a.final ?? a.proposed).find((a) => a?.action_type === "ESCALATE");
  if (fw?.team) return fw.team;
  const m = /to the (\w+) team/i.exec(ex?.detail ?? "");
  return m ? m[1].toLowerCase() : "support";
}

/** "Refund ₹1,497", "stop rental", "send to risk team" — what an action is, before it runs. */
export function actionShort(a: Pick<ProposedAction, "action_type" | "amount_paise" | "team">): string {
  switch (a.action_type) {
    case "REFUND_DEBITS":
      return `refund ${rupees(a.amount_paise ?? 0)}`;
    case "CANCEL_SUBSCRIPTION":
      return "stop rental";
    case "SCHEDULE_PICKUP":
      return "book pickup";
    case "SEND_KYC_LINK":
      return "send KYC link";
    case "RELEASE_HOLD":
      return "release hold";
    case "ESCALATE":
      return `send to ${a.team || "support"} team`;
    case "NO_ACTION":
      return "no action";
    default:
      return String(a.action_type ?? "").replace(/_/g, " ").toLowerCase();
  }
}

/** "₹998 refunded", "rental stopped" — what an executed action did. */
export function actionDone(view: CaseView, e: ExecutionView): string {
  switch (e.action_type) {
    case "REFUND_DEBITS":
      return `${rupees(e.amount_paise ?? 0)} refunded`;
    case "CANCEL_SUBSCRIPTION":
      return "rental stopped";
    case "SCHEDULE_PICKUP":
      return "pickup booked";
    case "SEND_KYC_LINK":
      return "KYC link sent";
    case "RELEASE_HOLD":
      return "hold released";
    case "ESCALATE":
      return `sent to ${escalationTeam(view, e)} team`;
    case "NO_ACTION":
      return "no action needed";
    default:
      return String(e.action_type).replace(/_/g, " ").toLowerCase();
  }
}

/** The thing itself, for "Waiting for approval: ₹2,495 refund" / "Blocked: release hold". */
function actionNoun(view: CaseView, e: ExecutionView): string {
  if (e.action_type === "REFUND_DEBITS") {
    const amt = e.amount_paise || proposedFor(view, e)?.amount_paise || 0;
    return `${rupees(amt)} refund`;
  }
  return actionShort({ action_type: e.action_type, team: escalationTeam(view, e) });
}

function proposedFor(view: CaseView, e: { action_type: string }): ProposedAction | null {
  const v = asArray(view.firewall?.actions).find((a) => a.proposed?.action_type === e.action_type);
  return v ? (v.final ?? v.proposed) : null;
}

// ───────────────────────────── status ─────────────────────────────

/** Case status, counting a pending approval that has since been decided. */
export function effectiveStatus(view: CaseView): string {
  const s = caseStatus(view);
  if (s === "AWAITING_APPROVAL" && !view.executions.some((e) => e.status === "PENDING_APPROVAL")) {
    return view.executions.some((e) => e.status === "REJECTED") ? "REJECTED" : "RESOLVED";
  }
  return s;
}

export type Tone = "run" | "ok" | "hum" | "esc" | "bad" | "gray";

export function statusTone(status: string): Tone {
  switch (status) {
    case "INVESTIGATING":
      return "run";
    case "RESOLVED":
      return "ok";
    case "AWAITING_APPROVAL":
      return "hum";
    case "ESCALATED":
      return "esc";
    case "BLOCKED":
    case "FAILED":
    case "REJECTED":
      return "bad";
    default:
      return "gray";
  }
}

/** Sidebar subtitle for a case row (only status/outcome known). */
export function rowSubtitle(row: Pick<CaseRow, "status" | "outcome">): string {
  switch (row.status) {
    case "INVESTIGATING":
      return "Working…";
    case "RESOLVED":
      return row.outcome === "REFUND_DUE" ? "Refunded" : row.outcome === "NO_FAULT" ? "No refund due" : "Resolved";
    case "AWAITING_APPROVAL":
      return "Waiting for approval";
    case "ESCALATED":
      return "Sent to a team";
    case "BLOCKED":
      return "Blocked";
    case "FAILED":
      return "Failed";
    default:
      return row.status ? cap(row.status.toLowerCase().replace(/_/g, " ")) : "";
  }
}

// ───────────────────────────── result card (chat) ─────────────────────────────

export interface ResultLine {
  tone: "ok" | "hum" | "warn" | "bad";
  text: string;
}
export interface ResultCard {
  lines: ResultLine[];
  tint: "" | "hum" | "warn" | "bad";
  fix: { from: string; to: string } | null;
}

export function resultCard(view: CaseView): ResultCard {
  const lines: ResultLine[] = [];
  if (view.verdict?.outcome === "NO_FAULT") lines.push({ tone: "ok", text: "No refund due · charge is correct" });
  for (const e of view.executions) {
    switch (e.status) {
      case "EXECUTED":
        if (e.action_type !== "NO_ACTION") lines.push({ tone: e.action_type === "ESCALATE" ? "warn" : "ok", text: cap(actionDone(view, e)) });
        break;
      case "PENDING_APPROVAL":
        lines.push({ tone: "hum", text: `Waiting for approval: ${actionNoun(view, e)}` });
        break;
      case "BLOCKED":
        lines.push({ tone: "bad", text: `Blocked: ${actionNoun(view, e)}` });
        break;
      case "REJECTED":
        lines.push({ tone: "bad", text: `Rejected: ${actionNoun(view, e)}` });
        break;
      case "FAILED":
        lines.push({ tone: "bad", text: `Failed: ${actionNoun(view, e)}` });
        break;
      default:
        break;
    }
  }
  const tint = lines.some((l) => l.tone === "bad") ? "bad" : lines.some((l) => l.tone === "hum") ? "hum" : lines.some((l) => l.tone === "warn") ? "warn" : "";
  const corrected = asArray(view.firewall?.actions).find((a) => a.decision === "CORRECTED" && a.proposed?.action_type === "REFUND_DEBITS" && a.final);
  const fix = corrected ? { from: rupees(corrected.proposed.amount_paise ?? 0), to: rupees(corrected.final?.amount_paise ?? 0) } : null;
  return { lines, tint, fix };
}

/** One-line summary of where the case ended up (sidebar subtitle of the open case). */
export function caseHeadline(view: CaseView): string {
  const s = effectiveStatus(view);
  if (s === "INVESTIGATING") return "Working…";
  if (s === "AWAITING_APPROVAL") return "Waiting for approval";
  const first = resultCard(view).lines[0];
  if (first) return first.text;
  return rowSubtitle({ status: s, outcome: view.done?.outcome ?? null });
}

// ───────────────────────────── steps ─────────────────────────────

export type StepIcon = "spark" | "search" | "code" | "flag" | "shield" | "coins" | "users";
export type StepState = "done" | "now" | "wait" | "skip" | "fix" | "err";

export interface StepRow {
  n: number;
  node: string;
  phase: string | null;
  kind: "ai" | "code";
  icon: StepIcon;
  name: string;
  state: StepState;
  result: string;
  time: string;
}

const STEP_DEFS: ReadonlyArray<{ node: string; phase: string | null; kind: "ai" | "code"; icon: StepIcon; name: string; role?: "reasoner" | "writer" | "decider" }> = [
  { node: "intake", phase: "Understand", kind: "ai", icon: "spark", name: "Read the complaint", role: "reasoner" },
  { node: "hunch", phase: null, kind: "ai", icon: "spark", name: "First hunch", role: "decider" },
  { node: "trail", phase: "Investigate", kind: "code", icon: "search", name: "Followed the money" },
  { node: "statements", phase: null, kind: "code", icon: "search", name: "Checked the claims" },
  { node: "lineup", phase: null, kind: "code", icon: "search", name: "Tested causes" },
  { node: "improvise", phase: null, kind: "ai", icon: "code", name: "Wrote own SQL", role: "reasoner" },
  { node: "verdict", phase: "Decide", kind: "code", icon: "flag", name: "Verdict" },
  { node: "propose", phase: null, kind: "ai", icon: "spark", name: "AI proposed a fix", role: "reasoner" },
  { node: "firewall", phase: null, kind: "code", icon: "shield", name: "Safety rules" },
  { node: "execute", phase: "Act", kind: "code", icon: "coins", name: "Carried it out" },
  { node: "respond", phase: "Reply", kind: "ai", icon: "spark", name: "Replied", role: "writer" },
  { node: "sweep", phase: null, kind: "code", icon: "users", name: "Checked others" },
];
export const STEP_COUNT = STEP_DEFS.length;

/** "Gemini" / "Sarvam" / "Scripted brain" from a provider name. */
export function brainName(provider: string | null | undefined, live?: boolean | null): string {
  const p = (provider ?? "").toLowerCase();
  if (!p || p === "mock" || live === false) return "Scripted brain";
  if (p.includes("gemini") || p.includes("google")) return "Gemini";
  if (p.includes("sarvam")) return "Sarvam";
  return cap(p);
}

function brainFor(view: CaseView, node: string, role: string | undefined, providers: ProviderSet | null): string {
  const s = view.llm[node];
  if (s && s.provider && s.provider !== "?") return brainName(s.provider, s.live);
  if (role === "decider") {
    const src = view.hunch?.source;
    if (src) return src === "mock" ? "Scripted brain" : "Sarvam";
  }
  const p = role ? providers?.[role as keyof ProviderSet] : undefined;
  return p ? brainName(p.provider, p.live) : "AI";
}

const INTENT_SHORT: Record<string, string> = { RENT_DEDUCTION: "rent cut", LOAN_DEDUCTION: "loan EDI", SETTLEMENT_ISSUE: "settlement", OTHER: "other" };

function statementCounts(checks: StatementCheck[]): string {
  const c = { VERIFIED: 0, PARTLY_TRUE: 0, CONTRADICTED: 0, UNVERIFIABLE: 0 } as Record<string, number>;
  for (const s of checks) c[s.status] = (c[s.status] ?? 0) + 1;
  const parts = [
    c.VERIFIED ? `${c.VERIFIED} true` : "",
    c.PARTLY_TRUE ? `${c.PARTLY_TRUE} partly` : "",
    c.CONTRADICTED ? `${c.CONTRADICTED} false` : "",
    c.UNVERIFIABLE ? `${c.UNVERIFIABLE} open` : "",
  ].filter(Boolean);
  return parts.join(" · ") || "No claims to check";
}

function stepResult(view: CaseView, node: string): string {
  switch (node) {
    case "intake": {
      const i = view.intake;
      if (!i) return "";
      const n = asArray(i.claims).length;
      return [langLabel(i.language), INTENT_SHORT[i.intent] ?? "other", plural(n, "claim"), i.injection_suspected ? "trick suspected" : ""].filter(Boolean).join(" · ");
    }
    case "hunch": {
      const h = view.hunch;
      if (!h?.top) return "";
      const p = h.probabilities?.[h.top];
      return `${causeTitle(h.top)}${typeof p === "number" ? ` · ${Math.round(p * 100)}%` : ""}`;
    }
    case "trail": {
      const disputed = asArray(view.trail?.nodes).filter((n) => asArray(n.flags).includes("DISPUTED"));
      if (!disputed.length) return "No disputed entries";
      if (disputed.every((n) => n.kind === "settlement")) return plural(disputed.length, "disputed settlement");
      const amts = new Set(disputed.map((n) => n.amount_paise ?? -1));
      if (disputed.every((n) => n.kind === "money") && amts.size === 1 && !amts.has(-1)) {
        return `${disputed.length} disputed ${rupees(disputed[0].amount_paise)} ${disputed.length === 1 ? "cut" : "cuts"}`;
      }
      return plural(disputed.length, "disputed entry", "disputed entries");
    }
    case "statements":
      return view.statements ? statementCounts(view.statements) : "";
    case "lineup": {
      if (!view.suspects.length) return "";
      const found = view.suspects.filter((s) => s.status === "MATCH").length;
      const out = view.suspects.filter((s) => s.status === "ELIMINATED").length;
      return `${found ? `${found} found` : "None matched"} · ${out} ruled out`;
    }
    case "improvise":
      return view.nodes.improvise?.status === "skip" ? "Skipped · not needed" : view.probes.length ? plural(view.probes.length, "read-only query", "read-only queries") : "";
    case "verdict": {
      const v = view.verdict;
      if (!v) return "";
      switch (v.outcome) {
        case "REFUND_DUE":
          return `${rupees(v.damages_paise ?? 0)} owed`;
        case "NO_FAULT":
          return "No fault · charge is correct";
        case "HOLD_EXPLAINED":
          return "Hold explained";
        case "IMPROVISED":
          return "Likely explained";
        case "NO_EVIDENCE":
          return "No evidence";
        default:
          return cap(String(v.outcome).toLowerCase().replace(/_/g, " "));
      }
    }
    case "propose": {
      const acts = asArray(view.proposal?.actions);
      return acts.length ? cap(acts.map(actionShort).join(" + ")) : "";
    }
    case "firewall": {
      const fw = view.firewall;
      if (!fw) return "";
      const parts = asArray(fw.actions)
        .filter((a) => a.decision !== "ALLOW")
        .map((a) => {
          const refund = a.proposed?.action_type === "REFUND_DEBITS";
          if (a.decision === "CORRECTED") return refund ? `Corrected to ${rupees(a.final?.amount_paise ?? 0)}` : `Corrected ${actionShort(a.proposed)}`;
          if (a.decision === "NEEDS_HUMAN") return refund ? `${rupees(a.proposed.amount_paise ?? 0)} needs a human` : `${cap(actionShort(a.proposed))} needs a human`;
          return `Blocked ${refund ? `${rupees(a.proposed.amount_paise ?? 0)} refund` : actionShort(a.proposed)}`;
        });
      return parts.join(" · ") || "Allowed";
    }
    case "execute": {
      if (!view.executions.length) return "";
      const done = view.executions.filter((e) => e.status === "EXECUTED").map((e) => actionDone(view, e));
      const waiting = view.executions.filter((e) => e.status === "PENDING_APPROVAL").length;
      const blocked = view.executions.filter((e) => e.status === "BLOCKED").length;
      const parts = [...done, waiting ? `${waiting} waiting for approval` : "", blocked ? `${blocked} blocked` : ""].filter(Boolean);
      return cap(parts.join(" · ")) || "Nothing to run";
    }
    case "respond": {
      const reply = view.messages.find((m) => m.role === "munim" && m.kind !== "chat" && m.final);
      if (!reply) return "";
      const words = reply.text.trim().split(/\s+/).filter(Boolean).length;
      return `${langLabel(reply.language ?? view.intake?.language)} · ${words} words`;
    }
    case "sweep": {
      const s = view.sweep;
      if (view.nodes.sweep?.status === "skip") return "No others found";
      if (!s) return "";
      return s.merchant_count > 0 ? `${s.merchant_count} more merchants hit` : "No others found";
    }
    default:
      return "";
  }
}

export interface StepsView {
  rows: StepRow[];
  /** 1-based step in progress (or the next one). */
  current: number;
  progress: number;
}

export function deriveSteps(view: CaseView, providers: ProviderSet | null): StepsView {
  let finished = 0;
  let running = 0;
  const rows = STEP_DEFS.map((d, i): StepRow => {
    const nv = view.nodes[d.node];
    const st = nv?.status ?? "pending";
    let state: StepState = "done";
    let result = "";
    let time = fmtMs(nv?.ms);
    if (st === "running") {
      state = "now";
      running = running || i + 1;
      result = d.kind === "ai" ? `${brainFor(view, d.node, d.role, providers)} · can't move money` : "Running…";
      time = "";
    } else if (st === "pending") {
      state = "wait";
      time = "";
    } else if (st === "skip") {
      state = "skip";
      finished += 1;
      result = stepResult(view, d.node) || "Skipped";
      time = "";
    } else if (st === "error") {
      state = "err";
      finished += 1;
      result = nv?.summary || "Error";
    } else {
      finished += 1;
      result = stepResult(view, d.node) || nv?.summary || "Done";
      if (d.node === "firewall" && view.firewall && view.firewall.decision !== "ALLOW") state = "fix";
    }
    return { n: i + 1, node: d.node, phase: d.phase, kind: d.kind, icon: d.icon, name: d.name, state, result, time };
  });
  const current = running || Math.min(STEP_COUNT, finished + 1);
  return { rows, current, progress: Math.min(1, finished / STEP_COUNT) };
}

/** What the typing bubble says while Munim works. */
export function phaseText(view: CaseView): string {
  const order = STEP_DEFS.map((d) => d.node);
  const node = view.currentNode ?? order.find((n) => (view.nodes[n]?.status ?? "pending") === "pending") ?? "intake";
  switch (node) {
    case "intake":
    case "hunch":
      return "Reading the complaint…";
    case "trail":
    case "statements":
    case "lineup":
    case "improvise":
      return "Checking the records…";
    case "verdict":
    case "propose":
    case "firewall":
      return "Deciding the fix…";
    case "execute":
      return "Carrying it out…";
    case "respond":
      return "Writing the reply…";
    default:
      return "Checking for others…";
  }
}

// ───────────────────────────── proof: money trail ─────────────────────────────

export interface TrailFlag {
  text: string;
  cls: "bad" | "ok" | "key" | "warn" | "gray";
}
export interface TrailRow {
  key: string;
  at: string | null;
  date: string;
  label: string;
  title: string;
  row: "" | "key" | "bad";
  flags: TrailFlag[];
  chip: string | null;
}

const refKey = (table: string, rowId: string) => `${table}:${rowId}`;
const segs = (label: string) => label.split(" · ").map((s) => s.trim());

/** "13 Nov 2026" inside a label → "2026-11-13" (for evidence that isn't on the trail). */
function dateFromLabel(label: string): string | null {
  const m = /(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})/.exec(label);
  if (!m) return null;
  return `${m[3]}-${String(MONTHS.indexOf(m[2]) + 1).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
}

function moneyLabel(label: string): string {
  const s = segs(label);
  const base = (s[0] ?? label).replace(/ debit$/, "");
  const t = /(\d{2}:\d{2})(?::\d{2})?$/.exec(s[1] ?? "");
  return t ? `${base} · ${t[1]}` : base;
}

function trailLabel(n: TrailNode): string {
  const s = segs(n.label);
  const flags = asArray(n.flags);
  switch (n.kind) {
    case "subscription":
      return flags.includes("DUPLICATE") ? "Second rental started" : "Rental started";
    case "money":
      return moneyLabel(n.label);
    case "edi_attempt":
      return `${cap(s[1] ?? "")} · ${(s[2] ?? "").replace(/(\d{2}:\d{2}):\d{2}/, "$1")}`;
    case "loan":
      return "Loan started";
    case "settlement": {
      const net = /net (₹[\d,.]+)/.exec(n.label)?.[1];
      const paid = !flags.includes("HELD") && !flags.includes("UNEXPLAINED_DEDUCTION");
      return `Settlement ${net ?? rupees(n.amount_paise)}${paid ? " · paid" : ""}`;
    }
    case "hold":
      return `${(s[0] ?? "").replace(/\s*HLD-[\w-]+/, "")} placed`;
    case "kyc":
      return `${s[0] ?? ""} ${(s[1] ?? "").split(/\s+/)[0]?.toLowerCase() ?? ""}`.trim();
    case "payments_summary": {
      const head = n.label.split(" (")[0].replace(/ via D\d+/, "").replace(/ batched into .*/, "");
      return /none after/.test(n.label) ? `${head} · none after pickup` : head;
    }
    case "device_event":
      return s[0] ?? n.label;
    case "pickup":
      return `Pickup ${(s[1] ?? "").toLowerCase()}`;
    default:
      return s[0] ?? n.label;
  }
}

/** The money trail as a short dated timeline: only what matters for this case. */
export function deriveTrail(view: CaseView): TrailRow[] {
  const nodes = asArray(view.trail?.nodes);
  const v = view.verdict;
  const gun = new Set(asArray(v?.smoking_gun).map((g) => refKey(g.table, g.row_id)));
  const damages = new Set(asArray(v?.damages_ledger_ids).map(String));
  const fairOutcome = v?.outcome === "REFUND_DUE" || v?.outcome === "NO_FAULT";
  const hasDeviceEvents = nodes.some((n) => n.kind === "device_event");
  const seenAwb = new Set<string>();
  const rows: TrailRow[] = [];

  for (const n of nodes) {
    const flags = asArray(n.flags);
    const inGun = gun.has(refKey(n.table, n.row_id));
    if (n.kind === "merchant" || n.kind === "mandate" || n.kind === "device") continue;
    if (n.kind === "pickup" && hasDeviceEvents) continue;
    if (flags.includes("SUMMARY") && !inGun) continue;
    if (n.kind === "device_event" && !inGun && !n.label.startsWith("Return requested")) continue;

    const out: TrailFlag[] = [];
    let row: TrailRow["row"] = "";
    const isLedger = n.table === "ledger_entries";
    if (isLedger && damages.has(n.row_id)) {
      out.push({ text: "WRONG", cls: "bad" });
      row = "bad";
    } else if (isLedger && flags.includes("DISPUTED") && fairOutcome) {
      out.push({ text: "FAIR", cls: "ok" });
    } else if (inGun && (n.kind !== "subscription" || flags.includes("DUPLICATE"))) {
      row = "key";
    }
    if (n.kind === "subscription" && flags.includes("DUPLICATE")) out.push({ text: "DUPLICATE", cls: "warn" });
    if (n.kind === "settlement" && flags.includes("HELD")) out.push({ text: "HELD", cls: "bad" });
    if (n.kind === "settlement" && flags.includes("UNEXPLAINED_DEDUCTION")) out.push({ text: "SHORT", cls: "warn" });
    if (n.kind === "kyc" && flags.includes("EXPIRED")) out.push({ text: "EXPIRED", cls: "bad" });
    if (flags.includes("FAILED") && n.kind === "device_event") out.push({ text: "FAILED", cls: "warn" });

    let chip: string | null = null;
    const awb = /AWB (\w+)/.exec(n.label)?.[1];
    if (inGun && awb && !seenAwb.has(awb)) {
      chip = `AWB ${awb}`;
      seenAwb.add(awb);
    }
    rows.push({ key: n.id, at: n.at ?? null, date: shortDate(n.at), label: trailLabel(n), title: n.label, row, flags: out, chip });
  }

  // Evidence the detective found off the trail (e.g. a chargeback found by its own SQL).
  const onTrail = new Set(nodes.map((n) => refKey(n.table, n.row_id)));
  for (const g of asArray(v?.smoking_gun) as EvidenceRef[]) {
    if (onTrail.has(refKey(g.table, g.row_id))) continue;
    const at = g.at ?? dateFromLabel(g.label);
    const label = g.table === "ledger_entries" ? moneyLabel(g.label) : (segs(g.label)[0] ?? g.label);
    rows.push({ key: `gun-${g.table}-${g.row_id}`, at, date: shortDate(at), label, title: g.label, row: "key", flags: [{ text: "FOUND", cls: "key" }], chip: null });
  }

  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => timeKey(a.r.at) - timeKey(b.r.at) || a.i - b.i)
    .map(({ r }) => r);
}

// ───────────────────────────── proof: claims ─────────────────────────────

export interface ClaimRow {
  status: "ok" | "bad" | "part" | "open";
  said: string;
  title: string;
  finding: string;
}

function monthOf(date: string): string {
  const m = /^(\d{4})-(\d{2})/.exec(date);
  if (!m) return date;
  const mon = MONTHS[Number(m[2]) - 1] ?? m[2];
  return m[1] === DEMO_YEAR ? mon : `${mon} ${m[1]}`;
}

function claimPhrase(c: StatementCheck["claim"]): string {
  switch (c.kind) {
    case "DEVICE_RETURNED":
      return `Returned the device${c.date ? ` in ${monthOf(c.date)}` : ""}`;
    case "DEDUCTION_AMOUNT":
      return c.amount_paise ? `${rupees(c.amount_paise)} was cut` : "Money was cut";
    case "DEDUCTION_COUNT":
      return c.count ? `Cut ${c.count} times` : "Cut many times";
    case "DUPLICATE_CHARGE":
      return "Charged twice";
    case "SINGLE_DEVICE":
      return "Has only one device";
    case "SETTLEMENT_SHORT":
      return c.amount_paise ? `${rupees(c.amount_paise)} missing from settlement` : "Settlement came short";
    case "SETTLEMENT_NOT_RECEIVED":
      return c.amount_paise ? `${rupees(c.amount_paise)} settlement not received` : "Settlement not received";
    case "REFUND_DEMANDED":
      return c.amount_paise ? `Wants ${rupees(c.amount_paise)} back` : "Wants the money back";
    default:
      return c.text;
  }
}

export function deriveClaims(view: CaseView): ClaimRow[] {
  const lang = view.intake?.language ?? view.info?.merchant?.language;
  return asArray(view.statements).map((s) => ({
    status: s.status === "VERIFIED" ? "ok" : s.status === "CONTRADICTED" ? "bad" : s.status === "PARTLY_TRUE" ? "part" : "open",
    said: claimPhrase(s.claim),
    title: isEnglish(lang) ? `Said: “${s.claim.text}”` : `Said in ${langLabel(lang)}: “${s.claim.text}”`,
    finding: s.finding.replace(/\s*\([^)]*\)/g, "").replace(/\s{2,}/g, " ").trim(),
  }));
}

// ───────────────────────────── proof: cause ─────────────────────────────

export interface CauseView {
  flag: TrailFlag;
  box: "" | "ok" | "warn" | "gray";
  title: string;
  headline: string;
  ruledOut: string[];
}

export function deriveCause(view: CaseView): CauseView | null {
  const v = view.verdict;
  if (!v) return null;
  const culprit = asArray(v.culprits)[0];
  let flag: TrailFlag = { text: "FOUND", cls: "key" };
  let box: CauseView["box"] = "";
  if (v.outcome === "NO_FAULT") {
    flag = { text: "NO FAULT", cls: "ok" };
    box = "ok";
  } else if (v.outcome === "HOLD_EXPLAINED") {
    flag = { text: "HOLD", cls: "warn" };
    box = "warn";
  } else if (v.outcome === "NO_EVIDENCE" || v.outcome === "UNSOLVED") {
    flag = { text: "NOTHING FOUND", cls: "gray" };
    box = "gray";
  }
  const title = culprit ? causeTitle(culprit) : v.outcome === "IMPROVISED" ? "Found by its own SQL" : "Nothing in the records supports it";
  const ruledOut = view.suspects.filter((s) => s.status === "ELIMINATED").map((s) => causeTitle(s.suspect_id));
  return { flag, box, title, headline: v.headline, ruledOut };
}

// ───────────────────────────── decision ─────────────────────────────

export interface CheckSquare {
  status: string;
  label: string;
  detail: string;
}
export interface RanLine {
  tone: "ok" | "hum" | "warn" | "bad";
  text: string;
  chips: string[];
  detail: string;
}
export interface DecisionView {
  proposed: { value: string; struck: boolean };
  rules: { small: string; value: string; cls: "fw" | "fwok" | "hum" | "bad" };
  end: { small: string; value: string; cls: "ok" | "hum" | "bad" | "wait" };
  why: string;
  checks: CheckSquare[];
  notes: CheckSquare[];
  ran: RanLine[];
  others: string[];
}

const DECISION_RANK: Record<string, number> = { CORRECTED: 0, NEEDS_HUMAN: 1, BLOCK: 2 };

function primaryVerdict(actions: ActionVerdict[]): ActionVerdict | null {
  if (!actions.length) return null;
  const ranked = actions
    .map((a, i) => ({ a, i, r: DECISION_RANK[a.decision] ?? (a.proposed?.action_type === "REFUND_DEBITS" ? 3 : 4) }))
    .sort((x, y) => x.r - y.r || x.i - y.i);
  return ranked[0].a;
}

function actionValue(a: ProposedAction | null | undefined): string {
  if (!a) return "—";
  return a.action_type === "REFUND_DEBITS" ? rupees(a.amount_paise ?? 0) : cap(actionShort(a));
}

function doneValue(type: string): string {
  switch (type) {
    case "REFUND_DEBITS":
      return "Refunded";
    case "CANCEL_SUBSCRIPTION":
      return "Rental stopped";
    case "SCHEDULE_PICKUP":
      return "Pickup booked";
    case "SEND_KYC_LINK":
      return "Link sent";
    case "RELEASE_HOLD":
      return "Released";
    case "ESCALATE":
      return "Sent";
    default:
      return "Done";
  }
}

function targetChips(view: CaseView, e: ExecutionView): string[] {
  const chips = asArray(e.ledger_entry_ids).map((id) => `#${id}`);
  const t = proposedFor(view, e)?.target_id;
  if (t) chips.push(t);
  return chips;
}

export function deriveDecision(view: CaseView): DecisionView | null {
  const actions = asArray(view.firewall?.actions);
  const p = primaryVerdict(actions);
  if (!p) return null;
  const type = p.proposed?.action_type;
  const ex = view.executions.find((e) => e.action_type === type);

  const proposed = { value: actionValue(p.proposed), struck: p.decision === "CORRECTED" || p.decision === "BLOCK" };
  let rules: DecisionView["rules"];
  if (p.decision === "CORRECTED") rules = { small: "Rules corrected", value: actionValue(p.final), cls: "fw" };
  else if (p.decision === "NEEDS_HUMAN") rules = { small: "Rules said", value: "Needs a human", cls: "hum" };
  else if (p.decision === "BLOCK") rules = { small: "Rules said", value: "Blocked", cls: "bad" };
  else rules = { small: "Rules allowed", value: actionValue(p.final ?? p.proposed), cls: "fwok" };

  let end: DecisionView["end"];
  switch (ex?.status) {
    case "EXECUTED":
      end = { small: ex.approver ? "Approved, done" : "Done", value: doneValue(String(type)), cls: "ok" };
      break;
    case "PENDING_APPROVAL":
      end = { small: "Now", value: "Waiting for you", cls: "hum" };
      break;
    case "REJECTED":
      end = { small: "Result", value: "Rejected", cls: "bad" };
      break;
    case "BLOCKED":
    case "FAILED":
      end = { small: "Result", value: "Not done", cls: "bad" };
      break;
    default:
      end = { small: "Next", value: "…", cls: "wait" };
  }

  const checks = asArray(p.checks) as FirewallCheck[];
  const pick = (st: string) => checks.find((c) => c.status === st);
  const whyCheck =
    p.decision === "CORRECTED" ? pick("FIXED") : p.decision === "NEEDS_HUMAN" ? pick("WARN") : p.decision === "BLOCK" ? (checks.find((c) => c.id === "hard_deny" && c.status === "FAIL") ?? pick("FAIL")) : undefined;
  const why = whyCheck ? aiWords(whyCheck.detail) : "Every rule passed, so it ran on its own.";
  const notes = checks.filter((c) => (c.status === "FIXED" || c.status === "WARN" || c.status === "FAIL") && c !== whyCheck).map((c) => ({ status: c.status, label: c.label, detail: aiWords(c.detail) }));

  const ran: RanLine[] = view.executions.map((e) => {
    const chips = targetChips(view, e);
    switch (e.status) {
      case "EXECUTED":
        return { tone: e.action_type === "ESCALATE" ? "warn" : "ok", text: cap(actionDone(view, e)), chips, detail: e.detail };
      case "PENDING_APPROVAL":
        return { tone: "hum", text: `Waiting: ${actionNoun(view, e)}`, chips, detail: e.detail };
      default:
        return { tone: "bad", text: `${cap(String(e.status).toLowerCase())}: ${actionNoun(view, e)}`, chips, detail: aiWords(e.detail) };
    }
  });

  const others = actions.filter((a) => a !== p).map((a) => `${cap(actionShort(a.proposed))} · ${a.decision === "ALLOW" ? "allowed" : a.decision.toLowerCase().replace("_", " ")}`);
  return { proposed, rules, end, why, checks: checks.map((c) => ({ status: c.status, label: c.label, detail: aiWords(c.detail) })), notes, ran, others };
}

// ───────────────────────────── needs you ─────────────────────────────

/** "₹2,495 is over the ₹2,000 auto-approval limit" from the firewall's WARN detail. */
export function approvalReason(reasons: (string | null)[] | null | undefined, checks?: FirewallCheck[] | null): string {
  const r = asArray(reasons).find((x): x is string => !!x) ?? asArray(checks).find((c) => c.status === "WARN")?.detail ?? "";
  return aiWords(r.split(", so")[0].replace(/\.$/, "")) || "Needs a human decision";
}
