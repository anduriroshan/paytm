// REST + SSE client. All URLs are relative (/api/...) so the same build works behind the Vite proxy
// and when FastAPI serves frontend/dist at "/".
import {
  EVENT_TYPES,
  type ApprovalRow,
  type ApproveResponse,
  type CaseDetail,
  type CaseRow,
  type Health,
  type InboxItem,
  type MerchantInfo,
  type Metrics,
  type MunimEvent,
  type PassbookRow,
  type Scenario,
  type SourcesList,
  type SweepApproveResponse,
  type SweepDetail,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), init?.timeoutMs ?? 20000);
  try {
    const res = await fetch(path, {
      ...init,
      signal: ctrl.signal,
      headers: init?.body instanceof FormData ? init?.headers : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const j = await res.json();
        if (j && typeof j.detail === "string") msg = j.detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(res.status, msg);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/api/health", { timeoutMs: 4000 }),
  scenarios: () => request<Scenario[]>("/api/scenarios"),
  /** The connected sources (backend/sources/): what Munim can reach, and when. */
  sources: () => request<SourcesList>("/api/sources"),
  merchants: (q = "") => request<MerchantInfo[]>(`/api/merchants${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  createCase: (body: { merchant_id: string; complaint_text: string; channel?: "chat" | "voice"; scenario_id?: string | null }) =>
    post<{ case_id: string }>("/api/cases", body),
  cases: () => request<CaseRow[]>("/api/cases"),
  /** Follow-up question on a finished case. Answers from the case file only; never moves money. */
  chat: (caseId: string, text: string) =>
    request<{ events: MunimEvent[] }>(`/api/cases/${encodeURIComponent(caseId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
      timeoutMs: 60000,
    }),
  caseDetail: (id: string) => request<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`),
  approvals: () => request<ApprovalRow[]>("/api/approvals"),
  approve: (actionId: number, approver = "ops.lead") => post<ApproveResponse>(`/api/approvals/${actionId}/approve`, { approver }),
  reject: (actionId: number, approver = "ops.lead", reason = "") =>
    post<{ result: ApproveResponse["result"] }>(`/api/approvals/${actionId}/reject`, { approver, reason }),
  /** Approve a sweep: only `merchantIds` when given (the ones the reviewer ticked), else every pending merchant. */
  approveSweep: (sweepId: string, merchantIds?: string[], approver = "ops.lead") =>
    post<SweepApproveResponse>(`/api/sweeps/${encodeURIComponent(sweepId)}/approve`,
      merchantIds ? { approver, merchant_ids: merchantIds } : { approver }),
  sweepDetail: (sweepId: string) => request<SweepDetail>(`/api/sweeps/${encodeURIComponent(sweepId)}`),
  /** A teammate sends Munim's draft (as written, or `text` if they edited it). Nothing reaches the merchant before. */
  sendReply: (caseId: string, seq: number | null, text?: string, by = "ops.lead") =>
    post<{ event: MunimEvent }>(`/api/cases/${encodeURIComponent(caseId)}/replies/send`,
      { seq, by, ...(text !== undefined ? { text } : {}) }),
  inbox: () => request<InboxItem[]>("/api/inbox"),
  metrics: () => request<Metrics>("/api/metrics"),
  passbook: (merchantId: string) => request<PassbookRow[]>(`/api/merchants/${encodeURIComponent(merchantId)}/passbook`),
  reset: () => post<{ counts: Record<string, number> }>("/api/reset"),
  stt: (audio: Blob, filename = "speech.webm") => {
    const fd = new FormData();
    fd.append("file", audio, filename);
    return request<{ text: string; language_code?: string; provider?: string }>("/api/voice/stt", {
      method: "POST",
      body: fd,
      timeoutMs: 60000,
    });
  },
  tts: (text: string, language: string) =>
    request<{ audio_base64: string; mime?: string; provider?: string }>("/api/voice/tts", {
      method: "POST",
      body: JSON.stringify({ text, language }),
      timeoutMs: 60000,
    }),
};

function parseEvent(raw: string): MunimEvent | null {
  try {
    const ev = JSON.parse(raw);
    return ev && typeof ev === "object" && typeof ev.type === "string" ? (ev as MunimEvent) : null;
  } catch {
    return null;
  }
}

export interface CaseStreamHandlers {
  /** Live events, in order. */
  onEvents: (events: MunimEvent[]) => void;
  /** Full history after a stream error: caller resets and folds these. */
  onRebuild: (events: MunimEvent[]) => void;
  onStatus?: (s: "connecting" | "live" | "reconnecting" | "closed") => void;
}

/**
 * Subscribe to GET /api/cases/{id}/events. Listens for every named event type AND onmessage.
 * Events are batched per animation-ish tick (16 ms) so token floods don't re-render per token.
 * On error: refetch GET /api/cases/{id}, rebuild from `events`, and reconnect unless the case is done.
 * Returns an unsubscribe function.
 */
export function subscribeCase(caseId: string, h: CaseStreamHandlers): () => void {
  let es: EventSource | null = null;
  let closed = false;
  let done = false;
  let buf: MunimEvent[] = [];
  let flushTimer: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let failures = 0;

  const flush = () => {
    flushTimer = null;
    if (!buf.length || closed) return;
    const out = buf;
    buf = [];
    h.onEvents(out);
  };

  const push = (raw: string) => {
    const ev = parseEvent(raw);
    if (!ev) return;
    buf.push(ev);
    if (ev.type === "done") {
      done = true;
      flush();
      es?.close();
      h.onStatus?.("closed");
      // The stream stops at `done`; chat turns and approvals recorded later live in the case file.
      api
        .caseDetail(caseId)
        .then((d) => {
          const later = (Array.isArray(d?.events) ? d.events : []).filter((x) => x && x.seq > ev.seq);
          if (!closed && later.length) h.onEvents(later);
        })
        .catch(() => undefined);
      return;
    }
    if (!flushTimer) flushTimer = setTimeout(flush, 16);
  };

  const open = () => {
    if (closed) return;
    h.onStatus?.(failures ? "reconnecting" : "connecting");
    es = new EventSource(`/api/cases/${encodeURIComponent(caseId)}/events`);
    es.onopen = () => {
      failures = 0;
      h.onStatus?.("live");
    };
    // `event: message` (our chat type) and unnamed events both arrive via onmessage; the rest are named.
    for (const t of EVENT_TYPES) {
      if (t !== "message") es.addEventListener(t, (e) => push((e as MessageEvent).data));
    }
    es.onmessage = (e) => push(e.data);
    es.onerror = () => {
      es?.close();
      es = null;
      if (closed || done) return;
      failures += 1;
      h.onStatus?.("reconnecting");
      flush();
      api
        .caseDetail(caseId)
        .then((detail) => {
          if (closed) return;
          const events = Array.isArray(detail?.events) ? detail.events : [];
          h.onRebuild(events);
          if (events.some((e) => e?.type === "done")) {
            done = true;
            h.onStatus?.("closed");
          }
        })
        .catch(() => {
          /* backend unreachable: keep what we have and retry */
        })
        .finally(() => {
          if (closed || done) return;
          retryTimer = setTimeout(open, Math.min(8000, 800 * failures));
        });
    };
  };

  open();
  return () => {
    closed = true;
    if (flushTimer) clearTimeout(flushTimer);
    if (retryTimer) clearTimeout(retryTimer);
    es?.close();
    es = null;
  };
}
