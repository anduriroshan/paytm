// Mirrors backend/munim/events.py and backend/munim/models.py.
// The backend may send nulls or omit fields; consumers must stay defensive.

// ───────────────────────────── events.py ─────────────────────────────

export const EVENT_TYPES = [
  "case",
  "node",
  "llm",
  "llm_done",
  "intake",
  "hunch",
  "trail",
  "sources",
  "statements",
  "suspect",
  "probe",
  "verdict",
  "proposal",
  "firewall",
  "execution",
  "message",
  "sweep",
  "approval",
  "error",
  "done",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

/** Graph nodes in display order: (node, agent, label). Mirrors events.NODES. */
export const NODES: ReadonlyArray<readonly [string, string, string]> = [
  ["intake", "Intake Agent", "Understand complaint"],
  ["hunch", "Detective", "Fake-instruction check (Sarvam Prism)"],
  ["trail", "Detective", "Follow the money trail"],
  ["statements", "Detective", "Check merchant's statements"],
  ["lineup", "Detective", "Suspect lineup"],
  ["improvise", "Detective", "Improvise probes (read-only SQL)"],
  ["verdict", "Detective", "Verdict"],
  ["propose", "Resolver Agent", "Propose action"],
  ["firewall", "Action Firewall", "Firewall (deterministic)"],
  ["execute", "Executor", "Execute / queue for approval"],
  ["respond", "Communicator Agent", "Reply to merchant"],
  ["sweep", "Detective", "Serial-offender sweep"],
];

export interface MunimEvent<T = Record<string, unknown>> {
  seq: number;
  case_id: string;
  type: EventType | string;
  at: string;
  data: T;
}

export interface MerchantInfo {
  id: string;
  business_name: string;
  owner_name?: string | null;
  city?: string | null;
  language?: string | null;
  category?: string | null;
}

export interface CaseEventData {
  case_id: string;
  merchant: MerchantInfo;
  complaint_text: string;
  channel?: "chat" | "voice" | string;
  scenario_id?: string | null;
}

export type NodeStatus = "start" | "end" | "skip" | "error";
export interface NodeEventData {
  node: string;
  agent?: string;
  status: NodeStatus;
  ms?: number | null;
  summary?: string | null;
}

export type LlmRole = "reasoner" | "writer" | string;
export interface LlmEventData {
  node: string;
  role: LlmRole;
  provider?: string;
  model?: string;
  kind: "content" | "reasoning";
  delta: string;
}

export interface LlmDoneEventData {
  node: string;
  role: LlmRole;
  provider?: string;
  model?: string;
  ms?: number;
  text?: string;
  usage?: Record<string, unknown> | null;
  live?: boolean;
  /** Not in the contract comment for llm_done, but LLMResult carries it; read if present. */
  fallback?: string | null;
  reasoning?: string | null;
}

/** A draft for the merchant, in their language (chat answers, when the teammate pasted the merchant's own words). */
export interface ReplyDraft {
  text: string;
  language: string | null;
}

export interface MessageEventData {
  /** "team": the support teammate's chat question (older recordings said "merchant" with kind "chat"). */
  role: "merchant" | "munim" | "team";
  text: string;
  language?: string | null;
  final?: boolean;
  /** English copy for the support team (the merchant's words read in English, or the reply in English). */
  en?: string | null;
  /** "chat" for questions and answers after the case is done; "update" for the message sent after an approval. */
  kind?: string | null;
  /** Chat answers: a reply to send the merchant, in their language. */
  reply?: ReplyDraft | null;
  /** Chat answers: short labels of the case-file facts the answer used. */
  sources?: string[] | null;
  /** Chat answers: the question is a different problem and needs a new chat. */
  new_case?: boolean | null;
  /** Team cases: Munim's reply or update waits on the case until a teammate presses Send. */
  draft?: boolean | null;
  /** kind "sent": the draft (event seq) a teammate sent, who sent it, and whether they edited it. */
  ref?: number | null;
  by?: string | null;
  edited?: boolean | null;
  reply_kind?: string | null;
}

export interface ApprovalEventData {
  action_id: number;
  status: string;
  result?: ExecutionResult | null;
  approver?: string | null;
}

export interface ErrorEventData {
  node?: string | null;
  message: string;
}

export interface DoneEventData {
  case_id: string;
  status?: string | null;
  outcome?: Outcome | string | null;
  decision?: Decision | string | null;
  ms?: number | null;
}

// ───────────────────────────── models.py ─────────────────────────────

export type Intent = "RENT_DEDUCTION" | "LOAN_DEDUCTION" | "SETTLEMENT_ISSUE" | "TRAVEL_REFUND" | "OTHER";

export type ClaimKind =
  | "DEVICE_RETURNED"
  | "DEDUCTION_AMOUNT"
  | "DEDUCTION_COUNT"
  | "DUPLICATE_CHARGE"
  | "SINGLE_DEVICE"
  | "SETTLEMENT_SHORT"
  | "SETTLEMENT_NOT_RECEIVED"
  | "TRIP_NOT_COMPLETED"
  | "REFUND_NOT_RECEIVED"
  | "REFUND_DEMANDED"
  | "OTHER";

export interface Claim {
  kind: ClaimKind | string;
  text: string;
  date?: string | null;
  amount_paise?: number | null;
  count?: number | null;
}

export interface IntakeResult {
  language: string;
  intent: Intent | string;
  summary_en: string;
  /** The complaint read in English. */
  text_en?: string | null;
  claims?: Claim[];
  injection_suspected?: boolean;
  sentiment?: "calm" | "frustrated" | "angry" | string;
}

export interface Hunch {
  source: string;
  probabilities: Record<string, number>;
  top: string;
  injection_score?: number;
  ms?: number;
}

export interface EvidenceRef {
  table: string;
  row_id: string;
  label: string;
  at?: string | null;
  amount_paise?: number | null;
}

export type TrailKind =
  | "merchant"
  | "money"
  | "subscription"
  | "mandate"
  | "device"
  | "device_event"
  | "booking"
  | "trip"
  | "trip_event"
  | "airline_refund"
  | "call"
  | "pickup"
  | "loan"
  | "edi_attempt"
  | "settlement"
  | "hold"
  | "kyc"
  | "payments_summary"
  | "chargeback"
  | "action";

export interface TrailNode {
  id: string;
  table: string;
  row_id: string;
  kind: TrailKind | string;
  label: string;
  at?: string | null;
  amount_paise?: number | null;
  flags?: string[];
  data?: Record<string, unknown>;
}

export interface TrailEdge {
  source: string;
  target: string;
  label: string;
}

export interface Trail {
  merchant_id: string;
  window_start: string;
  window_end: string;
  disputed?: string[];
  nodes?: TrailNode[];
  edges?: TrailEdge[];
}

export type StatementStatus = "VERIFIED" | "CONTRADICTED" | "PARTLY_TRUE" | "UNVERIFIABLE";
export interface StatementCheck {
  claim: Claim;
  status: StatementStatus | string;
  finding: string;
  evidence?: EvidenceRef[];
}

export type ActionType =
  | "REFUND_DEBITS"
  | "CANCEL_SUBSCRIPTION"
  | "SCHEDULE_PICKUP"
  | "SEND_KYC_LINK"
  | "RELEASE_HOLD"
  | "ESCALATE"
  | "NO_ACTION";

export type SuspectKind = "FAULT" | "INNOCENT" | "HOLD";
export type SuspectStatus = "MATCH" | "ELIMINATED" | "NOT_APPLICABLE";
export interface SuspectResult {
  suspect_id: string;
  title: string;
  kind: SuspectKind | string;
  status: SuspectStatus | string;
  summary: string;
  evidence?: EvidenceRef[];
  damages_ledger_ids?: number[];
  damages_paise?: number;
  targets?: Record<string, string>;
  remedies?: (ActionType | string)[];
  sweepable?: boolean;
  merchant_id?: string | null;
}

export interface Probe {
  step: number;
  purpose: string;
  sql: string;
  ok: boolean;
  columns?: string[];
  rows?: unknown[][];
  error?: string | null;
  ms?: number;
}

/** One connected source (backend/sources/*.yaml) and what Munim did with it for this case. */
export interface SourceHit {
  id: string;
  name: string;
  kind: "database" | "file" | "api" | string;
  reached: boolean;
  why?: string[];
  status?: "ok" | "error" | "not_needed" | string;
  records?: number;
  tables?: string[];
  columns?: string[];
  rows?: unknown[][];
  error?: string | null;
  ms?: number | null;
}

export interface SourcesReport {
  sources: SourceHit[];
  problems?: string[];
}

/** GET /api/sources: one row per source file. Never contains header values or API keys. */
export interface SourceListing {
  id: string;
  name: string;
  kind: "database" | "file" | "api" | string;
  enabled: boolean;
  file: string;
  holds: string;
  when: { always: boolean; topics: string[]; claims: string[]; words: string[] };
  tables: string[];
  actions: string[];
  blocked_actions: string[];
  max_rows: number;
  detail: { path?: string; url?: string; headers?: string[]; rows_at?: string; merchant_column?: string };
  warnings: string[];
}

export interface SourcesList {
  dir: string;
  sources: SourceListing[];
  problems: string[];
}

export type Outcome = "REFUND_DUE" | "NO_FAULT" | "HOLD_EXPLAINED" | "IMPROVISED" | "NO_EVIDENCE" | "UNSOLVED";

export interface Verdict {
  outcome: Outcome | string;
  culprits?: string[];
  headline: string;
  smoking_gun?: EvidenceRef[];
  damages_ledger_ids?: number[];
  damages_paise?: number;
  targets?: Record<string, string>;
  remedies?: (ActionType | string)[];
  confidence?: "HIGH" | "MEDIUM" | "LOW" | string;
  sweepable_suspect?: string | null;
}

export interface ProposedAction {
  action_type: ActionType | string;
  amount_paise?: number;
  ledger_entry_ids?: number[];
  target_id?: string | null;
  team?: string | null;
  reason?: string;
}

export interface Proposal {
  actions: ProposedAction[];
  rationale?: string;
}

export type Decision = "ALLOW" | "CORRECTED" | "NEEDS_HUMAN" | "BLOCK";
export type CheckStatus = "PASS" | "FAIL" | "FIXED" | "WARN" | "SKIP";

export interface FirewallCheck {
  id: string;
  label: string;
  status: CheckStatus | string;
  detail: string;
}

export interface ActionVerdict {
  proposed: ProposedAction;
  final: ProposedAction | null;
  decision: Decision | string;
  checks: FirewallCheck[];
  idempotency_key?: string | null;
}

export interface FirewallResult {
  decision: Decision | string;
  actions: ActionVerdict[];
}

export type ExecutionStatus = "EXECUTED" | "PENDING_APPROVAL" | "REJECTED" | "BLOCKED" | "FAILED" | "DUPLICATE";
export interface ExecutionResult {
  action_id?: number | null;
  action_type: ActionType | string;
  status: ExecutionStatus | string;
  amount_paise?: number;
  detail: string;
  ledger_entry_ids?: number[];
}

export interface SweepVictim {
  merchant_id: string;
  business_name: string;
  city: string;
  damages_paise: number;
  ledger_entry_ids?: number[];
  targets?: Record<string, string>;
}

export interface SweepResult {
  sweep_id: string | null;
  suspect_id: string;
  merchant_count: number;
  total_paise: number;
  victims?: SweepVictim[];
  action_ids?: number[];
}

export interface Scenario {
  id: string;
  title: string;
  merchant_id: string;
  language: string;
  channel?: "chat" | "voice";
  complaint_text: string;
  teaser: string;
  expect?: Record<string, unknown>;
  merchant?: MerchantInfo;
  /** how it reached support in the demo: call | email | ticket */
  via?: string;
  via_note?: string;
  received?: string;
}

// ───────────────────────────── REST payloads ─────────────────────────────

export interface ProviderInfo {
  provider?: string;
  model?: string;
  live?: boolean;
  note?: string | null;
}

export interface ProviderSet {
  reasoner?: ProviderInfo;
  writer?: ProviderInfo;
  decider?: ProviderInfo;
  voice?: ProviderInfo;
}

export interface Health {
  ok: boolean;
  today?: string;
  providers?: {
    reasoner?: ProviderInfo;
    writer?: ProviderInfo;
    decider?: ProviderInfo;
    voice?: ProviderInfo;
    [k: string]: ProviderInfo | undefined;
  };
  counts?: { merchants?: number; cases?: number };
}

export interface Metrics {
  cases?: number;
  resolved_auto?: number;
  awaiting_approval?: number;
  blocked_actions?: number;
  refunded_paise?: number;
  sweeps?: number;
  sweep_pending_paise?: number;
}

export interface CaseRow {
  id: string;
  merchant_id: string;
  business_name?: string;
  status: string;
  outcome?: string | null;
  language?: string | null;
  complaint_text: string;
  created_at: string;
  scenario_id?: string | null;
  channel?: string;
}

export interface CaseDetail {
  case: Record<string, unknown> & { id?: string; status?: string };
  events: MunimEvent[];
}

export interface ApprovalRow {
  action_id: number;
  case_id?: string | null;
  sweep_id?: string | null;
  merchant_id: string;
  business_name?: string;
  action_type: string;
  amount_paise?: number;
  params?: Record<string, unknown> | null;
  checks?: FirewallCheck[] | null;
  created_at?: string;
  // Extra fields returned by executor.pending() (not in the contract table; read if present).
  city?: string | null;
  proposed?: ProposedAction | null;
  reasons?: (string | null)[] | null;
  decision?: string | null;
  headline?: string | null;
  complaint_text?: string | null;
  sweep_suspect?: string | null;
}

export interface ApproveResponse {
  result: ExecutionResult;
  message?: { text: string; language?: string } | null;
}

/** GET /api/inbox: a case Munim worked, or a complaint nobody has opened yet (status "NEW", case_id null). */
export interface InboxItem {
  id: string;
  case_id: string | null;
  scenario_id?: string | null;
  merchant_id: string;
  business_name?: string | null;
  owner_name?: string | null;
  city?: string | null;
  language?: string | null;
  summary: string;
  text: string;
  /** How it reached support: call | email | ticket | chat | app. */
  via: string;
  via_note?: string;
  received?: string | null;
  created_at?: string | null;
  status: string;
  outcome?: string | null;
  approvals: number;
  /** Munim's reply: "draft" (waiting for Send), "sent", or null. */
  reply?: "draft" | "sent" | null;
}

export interface SweepLedgerRow {
  id: number;
  txn_ref: string;
  entry_type: string;
  amount_paise: number;
  status: string;
  value_date: string;
  narration: string;
}

export interface SweepMerchant {
  merchant_id: string;
  business_name: string;
  owner_name?: string;
  city?: string;
  amount_paise: number;
  status: string;
  evidence: EvidenceRef[];
  targets: Record<string, string>;
  ledger: SweepLedgerRow[];
  actions: { action_id: number; action_type: string; amount_paise: number; status: string; decided_by?: string | null;
    params?: Record<string, unknown>; checks: FirewallCheck[] }[];
}

/** GET /api/sweeps/{id}: what a reviewer checks before signing a sweep. */
export interface SweepDetail {
  sweep_id: string;
  suspect_id: string;
  status: string;
  created_at?: string;
  source_case?: { id: string; complaint_text: string; business_name: string } | null;
  merchant_count: number;
  pending: number;
  total_paise: number;
  merchants: SweepMerchant[];
}

export interface SweepApproveResponse {
  results: ExecutionResult[];
  executed: number;
  total_paise: number;
}

export interface PassbookRow {
  id: number;
  txn_ref: string;
  entry_type: string;
  direction: "DEBIT" | "CREDIT" | string;
  amount_paise: number;
  status: string;
  value_date: string;
  narration: string;
  refund_of?: number | null;
}
