// Pure selectors that reconstruct dashboard data from a CaseView, used in replay mode (no API calls).
import { asArray } from "../format";
import type { ApprovalRow, Metrics, PassbookRow, ProviderInfo, ProviderSet } from "../types";
import type { CaseView } from "./caseReducer";

function fromLlm(view: CaseView, role: string): ProviderInfo | undefined {
  const s = view.llmOrder.map((k) => view.llm[k]).find((x) => x && x.role === role && (x.provider || x.model));
  if (!s) return undefined;
  return { provider: s.provider || undefined, model: s.model || s.provider, live: s.live === true, note: s.fallback ? `fell back: ${s.fallback}` : null };
}

/** Provider chips for a recorded case: reasoner/writer from llm_done, decider from hunch.source. */
export function deriveProviders(view: CaseView): ProviderSet {
  const src = view.hunch?.source ?? "";
  let decider: ProviderInfo | undefined;
  if (src) {
    if (src === "mock") decider = { provider: "mock", model: "mock-prism", live: false };
    else if (src.startsWith("llm:")) decider = { provider: "llm", model: src.slice(4), live: true, note: "Prism unavailable — LLM fallback" };
    else decider = { provider: "sarvam", model: src, live: true };
  }
  return { reasoner: fromLlm(view, "reasoner"), writer: fromLlm(view, "writer"), decider };
}

/** Metrics for a recorded case (one case). `sweepExecuted` adds a locally simulated sweep approval. */
export function deriveMetrics(view: CaseView, sweepExecuted: { total_paise: number } | null): Metrics {
  if (!view.caseId) return {};
  const ex = view.executions;
  const pendingCase = ex.filter((e) => e.status === "PENDING_APPROVAL").length;
  const pendingSweep = sweepExecuted ? 0 : asArray(view.sweep?.action_ids).length;
  const refunded = ex.filter((e) => e.status === "EXECUTED" && e.action_type === "REFUND_DEBITS").reduce((a, e) => a + (e.amount_paise ?? 0), 0);
  const auto = view.done?.status === "RESOLVED" && pendingCase === 0 && !ex.some((e) => e.approver) ? 1 : 0;
  return {
    cases: 1,
    resolved_auto: auto,
    refunded_paise: refunded + (sweepExecuted?.total_paise ?? 0),
    blocked_actions: ex.filter((e) => e.status === "BLOCKED").length,
    awaiting_approval: pendingCase + pendingSweep,
  };
}

/** Pending case actions visible in the recording (sweep actions are shown on the sweep banner). */
export function deriveApprovals(view: CaseView): ApprovalRow[] {
  const mid = view.info?.merchant?.id ?? "";
  return view.executions
    .filter((e) => e.status === "PENDING_APPROVAL" && e.action_id != null)
    .map((e) => ({
      action_id: e.action_id as number,
      case_id: view.caseId,
      merchant_id: mid,
      business_name: view.info?.merchant?.business_name,
      action_type: e.action_type,
      amount_paise: e.amount_paise,
      checks: asArray(view.firewall?.actions).find((a) => (a.final ?? a.proposed)?.action_type === e.action_type)?.checks ?? [],
    }));
}

/** Passbook rows reconstructed from the trail's ledger rows plus executed refund credits. */
export function derivePassbook(view: CaseView): PassbookRow[] {
  const debits = asArray(view.trail?.nodes)
    .filter((n) => n.table === "ledger_entries")
    .map((n): PassbookRow => {
      const d = n.data ?? {};
      return {
        id: Number(n.row_id) || 0,
        txn_ref: typeof d.txn_ref === "string" ? d.txn_ref : n.row_id,
        entry_type: typeof d.entry_type === "string" ? d.entry_type : "DEBIT",
        direction: typeof d.direction === "string" ? d.direction : "DEBIT",
        amount_paise: n.amount_paise ?? 0,
        status: typeof d.status === "string" ? d.status : "SUCCESS",
        value_date: n.at ?? "",
        narration: n.label,
        refund_of: null,
      };
    });
  const byId = new Map(debits.map((r) => [r.id, r]));
  const today = (view.lastAt ?? "").slice(0, 10);
  const credits: PassbookRow[] = [];
  for (const e of view.executions) {
    if (e.status !== "EXECUTED" || e.action_type !== "REFUND_DEBITS") continue;
    const ids = asArray(e.ledger_entry_ids);
    const finalRefund = asArray(view.firewall?.actions).find((a) => a.final?.action_type === "REFUND_DEBITS")?.final;
    const refunded = asArray(finalRefund?.ledger_entry_ids);
    const split = ids.length > 0 && ids.length === refunded.length;
    if (split) {
      ids.forEach((cid, i) => {
        const of = byId.get(refunded[i]);
        credits.push({
          id: cid,
          txn_ref: `credit ${cid}`,
          entry_type: "REFUND_CREDIT",
          direction: "CREDIT",
          amount_paise: of?.amount_paise ?? 0,
          status: "SUCCESS",
          value_date: today,
          narration: `Refund of ${of ? of.narration : `ledger row ${refunded[i]}`}`,
          refund_of: refunded[i],
        });
      });
    } else {
      credits.push({
        id: ids[0] ?? -1,
        txn_ref: ids.length ? `credits ${ids.join(", ")}` : "credit",
        entry_type: "REFUND_CREDIT",
        direction: "CREDIT",
        amount_paise: e.amount_paise ?? 0,
        status: "SUCCESS",
        value_date: today,
        narration: e.detail || "Refund credit",
        refund_of: null,
      });
    }
  }
  return [...credits, ...debits].sort((a, b) => (b.value_date || "").localeCompare(a.value_date || "") || b.id - a.id);
}
