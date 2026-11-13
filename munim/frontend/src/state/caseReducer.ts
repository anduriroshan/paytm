// Pure event-sourced reducer: applyEvent(state, event) → new CaseView. No side effects, no mutation.
// Every live SSE event, a GET /api/cases/{id} rebuild and the offline replay all go through here.
import {
  NODES,
  type ApprovalEventData,
  type CaseEventData,
  type DoneEventData,
  type ErrorEventData,
  type ExecutionResult,
  type FirewallResult,
  type Hunch,
  type IntakeResult,
  type LlmDoneEventData,
  type LlmEventData,
  type MessageEventData,
  type MunimEvent,
  type NodeEventData,
  type Probe,
  type Proposal,
  type ReplyDraft,
  type StatementCheck,
  type SuspectResult,
  type SweepResult,
  type Trail,
  type Verdict,
} from "../types";

export type UiNodeStatus = "pending" | "running" | "done" | "skip" | "error";

export interface NodeView {
  node: string;
  agent: string;
  label: string;
  status: UiNodeStatus;
  ms: number | null;
  summary: string | null;
  startedAt: string | null;
}

export interface LlmStreamView {
  node: string;
  role: string;
  provider: string;
  model: string;
  content: string;
  reasoning: string;
  live: boolean | null;
  done: boolean;
  ms: number | null;
  fallback: string | null;
  /** How many LLM calls this node made (improvise can loop). */
  calls: number;
  firstSeq: number;
}

export interface ChatMessage {
  id: string;
  /** merchant: the complaint. team: the teammate's chat question. munim: reports, replies and answers. */
  role: "merchant" | "munim" | "team";
  text: string;
  language: string | null;
  /** Tokens still arriving. */
  streaming: boolean;
  /** A `message` event has confirmed the final text. */
  final: boolean;
  node: string | null;
  at: string | null;
  /** English copy for the team. Merchant: their words in English. Munim: the reply in English. */
  en?: string | null;
  /** "chat" for follow-up turns after the case is done. */
  kind?: string | null;
  sources?: string[];
  /** Chat answers: a reply to send the merchant, in their language. */
  reply?: ReplyDraft | null;
  newCase?: boolean;
}

export interface ExecutionView extends ExecutionResult {
  approver?: string | null;
  updatedBySeq?: number;
}

export interface CaseView {
  caseId: string | null;
  info: CaseEventData | null;
  startedAt: string | null;
  lastAt: string | null;
  lastSeq: number;
  eventCount: number;
  nodes: Record<string, NodeView>;
  nodeOrder: string[];
  currentNode: string | null;
  llm: Record<string, LlmStreamView>;
  llmOrder: string[];
  activeLlm: string | null;
  intake: IntakeResult | null;
  hunch: Hunch | null;
  trail: Trail | null;
  statements: StatementCheck[] | null;
  suspects: SuspectResult[];
  probes: Probe[];
  verdict: Verdict | null;
  proposal: Proposal | null;
  firewall: FirewallResult | null;
  executions: ExecutionView[];
  messages: ChatMessage[];
  sweep: SweepResult | null;
  approvals: ApprovalEventData[];
  errors: ErrorEventData[];
  done: DoneEventData | null;
}

function freshNodes(): Record<string, NodeView> {
  const out: Record<string, NodeView> = {};
  for (const [node, agent, label] of NODES) {
    out[node] = { node, agent, label, status: "pending", ms: null, summary: null, startedAt: null };
  }
  return out;
}

export function initialCaseView(): CaseView {
  return {
    caseId: null,
    info: null,
    startedAt: null,
    lastAt: null,
    lastSeq: -1,
    eventCount: 0,
    nodes: freshNodes(),
    nodeOrder: NODES.map(([n]) => n),
    currentNode: null,
    llm: {},
    llmOrder: [],
    activeLlm: null,
    intake: null,
    hunch: null,
    trail: null,
    statements: null,
    suspects: [],
    probes: [],
    verdict: null,
    proposal: null,
    firewall: null,
    executions: [],
    messages: [],
    sweep: null,
    approvals: [],
    errors: [],
    done: null,
  };
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v === null || v === undefined ? d : String(v));
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function upsertNode(state: CaseView, node: string, patch: Partial<NodeView>, agent?: string): CaseView {
  const prev: NodeView = state.nodes[node] ?? {
    node,
    agent: agent || "Agent",
    label: node,
    status: "pending",
    ms: null,
    summary: null,
    startedAt: null,
  };
  const next: NodeView = { ...prev, ...patch };
  const nodeOrder = state.nodeOrder.includes(node) ? state.nodeOrder : [...state.nodeOrder, node];
  return { ...state, nodes: { ...state.nodes, [node]: next }, nodeOrder };
}

function onNode(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<NodeEventData>;
  const node = str(d.node);
  if (!node) return state;
  const summary = d.summary === undefined || d.summary === null ? undefined : str(d.summary);
  const ms = num(d.ms);
  switch (d.status) {
    case "start":
      return {
        ...upsertNode(state, node, { status: "running", startedAt: ev.at ?? null, ...(summary ? { summary } : {}) }, d.agent),
        currentNode: node,
      };
    case "end": {
      const s = upsertNode(state, node, { status: "done", ms, ...(summary !== undefined ? { summary } : {}) }, d.agent);
      return { ...s, currentNode: state.currentNode === node ? null : state.currentNode };
    }
    case "skip": {
      const s = upsertNode(state, node, { status: "skip", ms, summary: summary ?? "Skipped" }, d.agent);
      return { ...s, currentNode: state.currentNode === node ? null : state.currentNode };
    }
    case "error": {
      const s = upsertNode(state, node, { status: "error", ms, summary: summary ?? "Error" }, d.agent);
      return { ...s, currentNode: state.currentNode === node ? null : state.currentNode };
    }
    default:
      return state;
  }
}

function streamKey(d: { node?: unknown; role?: unknown }): string {
  return str(d.node) || str(d.role) || "llm";
}

function onLlm(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<LlmEventData>;
  const key = streamKey(d);
  const delta = str(d.delta);
  const kind = d.kind === "reasoning" ? "reasoning" : "content";
  const prev = state.llm[key];
  let s: LlmStreamView;
  if (!prev) {
    s = {
      node: str(d.node) || key,
      role: str(d.role, "reasoner"),
      provider: str(d.provider, "?"),
      model: str(d.model, "?"),
      content: "",
      reasoning: "",
      live: null,
      done: false,
      ms: null,
      fallback: null,
      calls: 1,
      firstSeq: ev.seq,
    };
  } else if (prev.done) {
    // A further call on the same node (improvise loops): keep history, mark a new segment.
    const calls = prev.calls + 1;
    const sep = `\n\n── call ${calls} ──\n`;
    s = {
      ...prev,
      provider: str(d.provider, prev.provider),
      model: str(d.model, prev.model),
      content: prev.content ? prev.content + sep : prev.content,
      reasoning: prev.reasoning ? prev.reasoning + sep : prev.reasoning,
      done: false,
      calls,
    };
  } else {
    s = { ...prev, provider: str(d.provider, prev.provider), model: str(d.model, prev.model) };
  }
  s = kind === "reasoning" ? { ...s, reasoning: s.reasoning + delta } : { ...s, content: s.content + delta };
  const llmOrder = state.llmOrder.includes(key) ? state.llmOrder : [...state.llmOrder, key];
  let next: CaseView = { ...state, llm: { ...state.llm, [key]: s }, llmOrder, activeLlm: key };

  // Writer content tokens build the Munim reply bubble live.
  if (s.role === "writer" && kind === "content" && delta) {
    const idx = findOpenMunimMessage(next.messages, s.node);
    if (idx >= 0) {
      const msgs = next.messages.slice();
      msgs[idx] = { ...msgs[idx], text: msgs[idx].text + delta, streaming: true };
      next = { ...next, messages: msgs };
    } else {
      next = {
        ...next,
        messages: [
          ...next.messages,
          {
            id: `m-${ev.seq}`,
            role: "munim",
            text: delta,
            language: next.intake?.language ?? next.info?.merchant?.language ?? null,
            streaming: true,
            final: false,
            node: s.node,
            at: ev.at ?? null,
          },
        ],
      };
    }
  }
  return next;
}

/** Index of the last Munim message not yet confirmed by a `message` event (optionally for a node). */
function findOpenMunimMessage(msgs: ChatMessage[], node?: string | null): number {
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m.role === "munim" && !m.final && (node === undefined || node === null || m.node === node)) return i;
  }
  return -1;
}

function onLlmDone(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<LlmDoneEventData>;
  const key = streamKey(d);
  const prev = state.llm[key];
  const text = typeof d.text === "string" ? d.text : null;
  const base: LlmStreamView = prev ?? {
    node: str(d.node) || key,
    role: str(d.role, "reasoner"),
    provider: str(d.provider, "?"),
    model: str(d.model, "?"),
    content: "",
    reasoning: "",
    live: null,
    done: false,
    ms: null,
    fallback: null,
    calls: 1,
    firstSeq: ev.seq,
  };
  // If no tokens were streamed (non-streaming provider), show the final text.
  const content = base.content.trim() === "" && text ? text : base.content;
  const reasoning = base.reasoning.trim() === "" && typeof d.reasoning === "string" ? d.reasoning : base.reasoning;
  const s: LlmStreamView = {
    ...base,
    provider: str(d.provider, base.provider),
    model: str(d.model, base.model),
    content,
    reasoning,
    live: typeof d.live === "boolean" ? d.live : base.live,
    done: true,
    ms: num(d.ms) ?? base.ms,
    fallback: d.fallback ? str(d.fallback) : base.fallback,
  };
  const llmOrder = state.llmOrder.includes(key) ? state.llmOrder : [...state.llmOrder, key];
  let next: CaseView = { ...state, llm: { ...state.llm, [key]: s }, llmOrder };
  if (s.role === "writer") {
    const idx = findOpenMunimMessage(next.messages, s.node);
    if (idx >= 0) {
      const msgs = next.messages.slice();
      msgs[idx] = { ...msgs[idx], streaming: false };
      next = { ...next, messages: msgs };
    }
  }
  return next;
}

function onMessage(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<MessageEventData>;
  const text = str(d.text);
  const kind = d.kind ? str(d.kind) : null;
  // A chat question comes from the support teammate (older recordings said "merchant").
  const role = d.role === "munim" ? "munim" : d.role === "team" || kind === "chat" ? "team" : "merchant";
  const final = d.final !== false;
  let language = d.language ? str(d.language) : null;
  let body = text;
  const en = typeof d.en === "string" && d.en.trim() ? d.en : null;
  let reply = isObj(d.reply) && typeof d.reply.text === "string" && d.reply.text.trim()
    ? { text: d.reply.text, language: typeof d.reply.language === "string" ? d.reply.language : null }
    : null;
  if (role === "munim" && kind === "chat" && !reply && en && language && !language.toLowerCase().startsWith("en")) {
    // Older recordings answered in the merchant's language: the English is the answer, the original is the reply.
    reply = { text, language };
    body = en;
    language = "en";
  }
  const extra = {
    en,
    kind,
    sources: Array.isArray(d.sources) ? d.sources.filter((x): x is string => typeof x === "string" && !!x.trim()) : [],
    newCase: d.new_case === true,
    reply,
  };
  if (role !== "munim") {
    // The `case` event already showed the complaint; a chat question with the same words is a new turn.
    const isComplaint = role === "merchant" && !!state.info && text.trim() === state.info.complaint_text.trim();
    if (isComplaint) {
      const msgs = state.messages.map((m) =>
        m.role === "merchant" && m.kind !== "chat" && m.text.trim() === text.trim() ? { ...m, en: extra.en ?? m.en, language: language ?? m.language } : m,
      );
      return { ...state, messages: msgs };
    }
    return {
      ...state,
      messages: [
        ...state.messages,
        { id: `m-${ev.seq}`, role, text, language, streaming: false, final: true, node: null, at: ev.at ?? null, ...extra },
      ],
    };
  }
  const idx = findOpenMunimMessage(state.messages);
  if (idx >= 0) {
    const msgs = state.messages.slice();
    msgs[idx] = { ...msgs[idx], text: body || msgs[idx].text, language: language ?? msgs[idx].language, streaming: !final, final, ...extra };
    return { ...state, messages: msgs };
  }
  return {
    ...state,
    messages: [
      ...state.messages,
      { id: `m-${ev.seq}`, role, text: body, language, streaming: !final, final, node: null, at: ev.at ?? null, ...extra },
    ],
  };
}

function onCase(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<CaseEventData>;
  const merchant = isObj(d.merchant) ? (d.merchant as unknown as CaseEventData["merchant"]) : { id: "?", business_name: "Unknown merchant" };
  const info: CaseEventData = {
    case_id: str(d.case_id, ev.case_id),
    merchant,
    complaint_text: str(d.complaint_text),
    channel: str(d.channel, "chat"),
    scenario_id: d.scenario_id ?? null,
  };
  const hasMerchantMsg = state.messages.some((m) => m.role === "merchant" && m.text.trim() === info.complaint_text.trim());
  const messages = hasMerchantMsg || !info.complaint_text
    ? state.messages
    : [
        {
          id: `m-${ev.seq}`,
          role: "merchant" as const,
          text: info.complaint_text,
          language: merchant.language ?? null,
          streaming: false,
          final: true,
          node: null,
          at: ev.at ?? null,
        },
        ...state.messages,
      ];
  return { ...state, caseId: info.case_id || state.caseId, info, messages, startedAt: state.startedAt ?? ev.at ?? null };
}

function onExecution(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as { results?: unknown };
  const results = Array.isArray(d.results) ? (d.results as ExecutionResult[]) : [];
  let executions = state.executions.slice();
  for (const r of results) {
    if (!isObj(r)) continue;
    const i = r.action_id != null ? executions.findIndex((e) => e.action_id === r.action_id) : -1;
    if (i >= 0) executions[i] = { ...executions[i], ...r, updatedBySeq: ev.seq };
    else executions = [...executions, { ...r, updatedBySeq: ev.seq }];
  }
  return { ...state, executions };
}

function onApproval(state: CaseView, ev: MunimEvent): CaseView {
  const d = ev.data as Partial<ApprovalEventData>;
  if (d.action_id === undefined || d.action_id === null) return state;
  const approval: ApprovalEventData = {
    action_id: d.action_id,
    status: str(d.status, "?"),
    result: isObj(d.result) ? (d.result as unknown as ExecutionResult) : null,
    approver: d.approver ?? null,
  };
  const approvals = [...state.approvals.filter((a) => a.action_id !== approval.action_id), approval];
  let executions = state.executions;
  const i = executions.findIndex((e) => e.action_id === approval.action_id);
  const patch = approval.result ?? { status: approval.status };
  if (i >= 0) {
    executions = executions.slice();
    executions[i] = { ...executions[i], ...patch, approver: approval.approver, updatedBySeq: ev.seq };
  } else if (approval.result) {
    executions = [...executions, { ...approval.result, approver: approval.approver, updatedBySeq: ev.seq }];
  }
  return { ...state, approvals, executions };
}

/** Apply one event. Pure: returns a new state (or the same object when the event is ignored). */
export function applyEvent(state: CaseView, ev: MunimEvent | null | undefined): CaseView {
  if (!ev || !isObj(ev) || typeof ev.type !== "string") return state;
  const seq = typeof ev.seq === "number" ? ev.seq : null;
  if (seq !== null && seq <= state.lastSeq) return state; // already applied (SSE replays history)
  if (state.caseId && ev.case_id && ev.case_id !== state.caseId) return state; // stale stream for another case
  const data = isObj(ev.data) ? ev.data : {};
  const e: MunimEvent = { ...ev, data };

  let s: CaseView = {
    ...state,
    lastSeq: seq ?? state.lastSeq,
    eventCount: state.eventCount + 1,
    lastAt: ev.at ?? state.lastAt,
    startedAt: state.startedAt ?? ev.at ?? null,
    caseId: state.caseId ?? (ev.case_id || null),
  };

  switch (e.type) {
    case "case":
      return onCase(s, e);
    case "node":
      return onNode(s, e);
    case "llm":
      return onLlm(s, e);
    case "llm_done":
      return onLlmDone(s, e);
    case "intake":
      return { ...s, intake: data as unknown as IntakeResult };
    case "hunch":
      return { ...s, hunch: { ...(data as unknown as Hunch), probabilities: isObj(data.probabilities) ? (data.probabilities as Record<string, number>) : {} } };
    case "trail":
      return { ...s, trail: data as unknown as Trail };
    case "statements":
      return { ...s, statements: Array.isArray(data.checks) ? (data.checks as StatementCheck[]) : [] };
    case "suspect": {
      const r = data as unknown as SuspectResult;
      if (!r.suspect_id) return s;
      const i = s.suspects.findIndex((x) => x.suspect_id === r.suspect_id);
      const suspects = i >= 0 ? s.suspects.map((x, j) => (j === i ? r : x)) : [...s.suspects, r];
      return { ...s, suspects };
    }
    case "probe": {
      const p = data as unknown as Probe;
      const i = s.probes.findIndex((x) => x.step === p.step);
      const probes = i >= 0 ? s.probes.map((x, j) => (j === i ? p : x)) : [...s.probes, p];
      return { ...s, probes };
    }
    case "verdict":
      return { ...s, verdict: data as unknown as Verdict };
    case "proposal":
      return { ...s, proposal: { ...(data as unknown as Proposal), actions: Array.isArray(data.actions) ? (data.actions as Proposal["actions"]) : [] } };
    case "firewall":
      return { ...s, firewall: { ...(data as unknown as FirewallResult), actions: Array.isArray(data.actions) ? (data.actions as FirewallResult["actions"]) : [] } };
    case "execution":
      return onExecution(s, e);
    case "message":
      return onMessage(s, e);
    case "sweep":
      return { ...s, sweep: data as unknown as SweepResult };
    case "approval":
      return onApproval(s, e);
    case "error": {
      const err: ErrorEventData = { node: data.node ? str(data.node) : null, message: str(data.message, "Unknown error") };
      s = { ...s, errors: [...s.errors, err] };
      if (err.node) s = upsertNode(s, err.node, { status: "error", summary: err.message });
      return s;
    }
    case "done": {
      const done = data as unknown as DoneEventData;
      // Close any node still marked running and any open stream.
      const nodes: Record<string, NodeView> = {};
      for (const [k, v] of Object.entries(s.nodes)) nodes[k] = v.status === "running" ? { ...v, status: "done" } : v;
      const messages = s.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m));
      return { ...s, done, nodes, messages, currentNode: null };
    }
    default:
      return s; // unknown type: counted, otherwise ignored
  }
}

export function applyEvents(state: CaseView, events: readonly MunimEvent[]): CaseView {
  let s = state;
  for (const ev of events) s = applyEvent(s, ev);
  return s;
}

export function buildCaseView(events: readonly MunimEvent[]): CaseView {
  return applyEvents(initialCaseView(), [...events].sort((a, b) => (a?.seq ?? 0) - (b?.seq ?? 0)));
}

// ───────────────────────────── selectors ─────────────────────────────

export type CaseStatus = "IDLE" | "INVESTIGATING" | string;

export function caseStatus(v: CaseView): CaseStatus {
  if (!v.caseId) return "IDLE";
  if (v.done) return v.done.status || "DONE";
  if (v.errors.length && !v.currentNode) return "FAILED";
  return "INVESTIGATING";
}

/** Total ms elapsed between first and last event, for the case clock. */
export function elapsedMs(v: CaseView): number | null {
  if (v.done?.ms != null) return v.done.ms;
  if (!v.startedAt || !v.lastAt) return null;
  const a = Date.parse(v.startedAt);
  const b = Date.parse(v.lastAt);
  return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, b - a) : null;
}
