// What each of the 12 steps actually did, for the expandable rows in "How Munim decided" → Steps.
// Built only from the CaseView (the case's own events): no API calls, nothing per scenario.
import { asArray, fmtDate, langLabel, pct, rupees } from "../format";
import type { CaseView } from "../state/caseReducer";
import type { EvidenceRef } from "../types";
import { actionShort, aiWords, brainName, cap, causeTitle, deriveClaims, peopleWord, shortDate } from "./explain";

export type DetailTone = "ok" | "bad" | "warn" | "key" | "muted";

export interface DetailLine {
  /** Short left label ("Language"). Without one, the line is a list item. */
  label?: string;
  text: string;
  tag?: { text: string; tone: DetailTone };
  /** Right-aligned value (an amount, a row count). */
  right?: string;
  /** Second line, muted. */
  sub?: string;
  /** Small mono chips: "ledger_entries #112". */
  refs?: string[];
  /** A code block (read-only SQL). */
  code?: string;
  /** 0..1, drawn as a thin bar (hunch probabilities). */
  bar?: number;
}

export interface DetailGroup {
  title?: string;
  lines: DetailLine[];
  /** How many more items exist than are shown. */
  more?: number;
}

export interface StepDetail {
  /** "AI · Gemini · gemini-flash-latest" or "Plain code · no AI". */
  by: string;
  ai: boolean;
  /** One sentence: what this step does. */
  what: string;
  groups: DetailGroup[];
  /** The AI's own output, for anyone who wants to read it. */
  raw?: { label: string; text: string };
}

const CODE = "Plain code · no AI";
const MAX_LIST = 8;

const ref = (e: Pick<EvidenceRef, "table" | "row_id">) => `${e.table} #${e.row_id}`;
const firstSeg = (label: string) => label.split(" · ")[0]?.trim() || label;
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function capped<T>(items: T[], n = MAX_LIST): { shown: T[]; more?: number } {
  return items.length > n ? { shown: items.slice(0, n), more: items.length - n } : { shown: items };
}

/** "AI · Gemini · gemini-flash-latest · 2 calls" from the node's LLM stream. */
function aiBy(view: CaseView, node: string): string {
  const s = view.llm[node];
  if (!s) return "AI";
  const brain = brainName(s.provider, s.live);
  if (brain === "Scripted brain") return "Scripted brain (offline stand-in for the AI)";
  const model = s.model && s.model !== "?" ? s.model : "";
  return ["AI", brain, model, s.calls > 1 ? `${s.calls} calls` : "", s.fallback ? `fallback: ${s.fallback}` : ""].filter(Boolean).join(" · ");
}

/** JSON output is re-indented for reading; anything else is shown as it came. */
function pretty(text: string): string {
  if (!/^[[{]/.test(text)) return text;
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function aiRaw(view: CaseView, node: string): StepDetail["raw"] {
  const text = view.llm[node]?.content?.trim();
  return text ? { label: "AI output (as returned)", text: pretty(text) } : undefined;
}

const INTENT: Record<string, string> = { RENT_DEDUCTION: "Rent deduction", LOAN_DEDUCTION: "Loan instalment", SETTLEMENT_ISSUE: "Settlement", OTHER: "Other" };

const CLAIM_KIND: Record<string, string> = {
  DEVICE_RETURNED: "Device returned",
  DEDUCTION_AMOUNT: "Amount cut",
  DEDUCTION_COUNT: "Times cut",
  DUPLICATE_CHARGE: "Charged twice",
  SINGLE_DEVICE: "One device",
  SETTLEMENT_SHORT: "Settlement short",
  SETTLEMENT_NOT_RECEIVED: "Settlement missing",
  REFUND_DEMANDED: "Wants a refund",
  OTHER: "Other",
};

const STATEMENT_TAG: Record<string, { text: string; tone: DetailTone }> = {
  VERIFIED: { text: "True", tone: "ok" },
  PARTLY_TRUE: { text: "Partly", tone: "warn" },
  CONTRADICTED: { text: "Not true", tone: "bad" },
  UNVERIFIABLE: { text: "Can't check", tone: "muted" },
};

const SUSPECT_TAG: Record<string, { text: string; tone: DetailTone; rank: number }> = {
  MATCH: { text: "Found", tone: "key", rank: 0 },
  ELIMINATED: { text: "Ruled out", tone: "muted", rank: 1 },
  NOT_APPLICABLE: { text: "Not relevant", tone: "muted", rank: 2 },
};

const OUTCOME: Record<string, string> = {
  REFUND_DUE: "Refund due",
  NO_FAULT: "No fault · the charge is correct",
  HOLD_EXPLAINED: "Hold explained",
  IMPROVISED: "Explained by its own SQL",
  NO_EVIDENCE: "No evidence in the records",
  UNSOLVED: "Unsolved",
};

const DECISION_TAG: Record<string, { text: string; tone: DetailTone }> = {
  ALLOW: { text: "Allowed", tone: "ok" },
  CORRECTED: { text: "Corrected", tone: "warn" },
  NEEDS_HUMAN: { text: "Needs a person", tone: "warn" },
  BLOCK: { text: "Blocked", tone: "bad" },
};

const CHECK_TAG: Record<string, { text: string; tone: DetailTone; rank: number }> = {
  FAIL: { text: "Fail", tone: "bad", rank: 0 },
  FIXED: { text: "Fixed", tone: "warn", rank: 1 },
  WARN: { text: "Warn", tone: "warn", rank: 2 },
  PASS: { text: "Pass", tone: "ok", rank: 3 },
  SKIP: { text: "Skip", tone: "muted", rank: 4 },
};

const EXEC_TAG: Record<string, { text: string; tone: DetailTone }> = {
  EXECUTED: { text: "Done", tone: "ok" },
  PENDING_APPROVAL: { text: "Waiting for approval", tone: "warn" },
  REJECTED: { text: "Rejected", tone: "bad" },
  BLOCKED: { text: "Blocked", tone: "bad" },
  FAILED: { text: "Failed", tone: "bad" },
  DUPLICATE: { text: "Already done", tone: "muted" },
};

const KIND_LABEL: Record<string, string> = { database: "Database", file: "File", api: "API" };

const tagOf = <T extends { text: string; tone: DetailTone }>(map: Record<string, T>, key: string): { text: string; tone: DetailTone } => {
  const t = map[key];
  return t ? { text: t.text, tone: t.tone } : { text: cap(String(key ?? "").replace(/_/g, " ").toLowerCase()), tone: "muted" };
};

// ───────────────────────────── per step ─────────────────────────────

function intake(view: CaseView): StepDetail | null {
  const i = view.intake;
  const base = { by: aiBy(view, "intake"), ai: true, what: "Reads the complaint and turns it into claims the records can check.", raw: aiRaw(view, "intake") };
  if (!i) return view.llm.intake ? { ...base, groups: [] } : null;
  const lines: DetailLine[] = [
    { label: "Language", text: langLabel(i.language) },
    { label: "Topic", text: INTENT[i.intent] ?? cap(String(i.intent).toLowerCase().replace(/_/g, " ")) },
  ];
  if (i.sentiment) lines.push({ label: "Mood", text: cap(i.sentiment) });
  lines.push({
    label: "Trick check",
    text: i.injection_suspected ? "Instructions hidden in the complaint" : "Nothing suspicious",
    tag: i.injection_suspected ? { text: "Flagged", tone: "bad" } : undefined,
  });
  if (i.summary_en) lines.push({ label: "Summary", text: i.summary_en });
  const claims = asArray(i.claims).map((c): DetailLine => {
    const facts = [c.amount_paise ? rupees(c.amount_paise) : "", c.date ? fmtDate(c.date) : "", c.count ? `${c.count} times` : ""].filter(Boolean).join(" · ");
    const words = c.text ? `“${c.text}”` : "";
    return { label: CLAIM_KIND[c.kind] ?? cap(String(c.kind).toLowerCase().replace(/_/g, " ")), text: facts || words || "—", sub: facts && words ? words : undefined };
  });
  const groups: DetailGroup[] = [{ lines }];
  if (claims.length) groups.push({ title: `Claims to check (${claims.length})`, lines: claims });
  return { ...base, groups };
}

function hunch(view: CaseView): StepDetail | null {
  const h = view.hunch;
  if (!h) return null;
  const scripted = !h.source || h.source === "mock";
  const top = Object.entries(h.probabilities ?? {})
    .filter(([, p]) => typeof p === "number")
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([id, p]): DetailLine => ({ text: causeTitle(id), right: pct(p), bar: p }));
  const groups: DetailGroup[] = [];
  if (typeof h.injection_score === "number") {
    groups.push({
      lines: [{ label: "Fake-instruction score", text: pct(h.injection_score), tag: h.injection_score >= 0.5 ? { text: "High", tone: "bad" } : undefined, sub: "The safety rules read this score." }],
    });
  }
  if (top.length) groups.push({ title: "Guess at the cause (shown only, not used)", lines: top });
  return {
    by: scripted ? "Scripted brain (offline stand-in for the AI)" : `AI · ${brainName(h.source)} · ${h.source}`,
    ai: true,
    what: "Scores how likely the complaint is trying to give the system orders. Only this score is used, by the safety rules. Causes are found from the records.",
    groups,
  };
}

function trail(view: CaseView): StepDetail | null {
  const t = view.trail;
  const src = view.sources;
  if (!t && !src) return null;
  const groups: DetailGroup[] = [];
  if (src) {
    const reached = src.sources.filter((s) => s.reached);
    const skipped = src.sources.filter((s) => !s.reached);
    groups.push({
      title: `Sources read (${reached.length})`,
      lines: reached.map(
        (s): DetailLine => ({
          text: s.name,
          tag: s.status === "error" ? { text: "Error", tone: "bad" } : undefined,
          right: s.status === "error" ? undefined : plural(s.records ?? 0, "record"),
          sub: [KIND_LABEL[s.kind] ?? cap(s.kind), (s.why ?? []).length ? `why: ${(s.why ?? []).join(", ")}` : "", s.error ?? ""].filter(Boolean).join(" · "),
          refs: s.tables ?? [],
        }),
      ),
    });
    const extra: DetailLine[] = [];
    if (skipped.length) extra.push({ label: "Not needed", text: skipped.map((s) => s.name).join(" · ") });
    for (const p of src.problems ?? []) extra.push({ label: "Problem", text: p, tag: { text: "Check", tone: "warn" } });
    if (extra.length) groups.push({ lines: extra });
  }
  if (t) {
    const nodes = asArray(t.nodes);
    const byTable = new Map<string, number>();
    for (const n of nodes) byTable.set(n.table, (byTable.get(n.table) ?? 0) + 1);
    groups.push({
      title: `Records linked (${nodes.length})`,
      lines: [
        { label: "Window", text: `${fmtDate(t.window_start)} – ${fmtDate(t.window_end)}` },
        { label: "Tables", text: "", refs: [...byTable].map(([table, n]) => `${table} × ${n}`) },
      ],
    });
    const disputed = nodes.filter((n) => asArray(n.flags).includes("DISPUTED"));
    if (disputed.length) {
      const { shown, more } = capped(disputed);
      groups.push({
        title: `Disputed entries (${disputed.length})`,
        lines: shown.map((n): DetailLine => ({ label: shortDate(n.at), text: firstSeg(n.label), right: `#${n.row_id}`, tag: asArray(n.flags).includes("DUPLICATE") ? { text: "Duplicate", tone: "warn" } : undefined })),
        more,
      });
    }
  }
  return { by: CODE, ai: false, what: "Reads this merchant's records from the connected sources and links them into one money trail.", groups };
}

function statements(view: CaseView): StepDetail | null {
  const checks = view.statements;
  if (!checks) return null;
  const said = deriveClaims(view);
  return {
    by: CODE,
    ai: false,
    what: "Checks each thing the merchant said against the records.",
    groups: [
      {
        lines: checks.map(
          (c, i): DetailLine => ({
            text: said[i]?.said ?? c.claim.text,
            tag: tagOf(STATEMENT_TAG, c.status),
            sub: c.finding,
            refs: asArray(c.evidence).slice(0, 6).map(ref),
          }),
        ),
      },
    ],
  };
}

function lineup(view: CaseView): StepDetail | null {
  if (!view.suspects.length) return null;
  const rank = (s: string) => SUSPECT_TAG[s]?.rank ?? 3;
  const sorted = [...view.suspects].sort((a, b) => rank(a.status) - rank(b.status));
  return {
    by: CODE,
    ai: false,
    what: `Tests ${plural(sorted.length, "known cause")} against the records, one rule each.`,
    groups: [
      {
        lines: sorted.map(
          (s): DetailLine => ({
            text: causeTitle(s.suspect_id) || s.title,
            tag: tagOf(SUSPECT_TAG, s.status),
            sub: s.summary,
            refs: asArray(s.evidence).slice(0, 6).map(ref),
            right: s.status === "MATCH" && s.damages_paise ? rupees(s.damages_paise) : undefined,
          }),
        ),
      },
    ],
  };
}

function columnsLine(cols: string[] | undefined): string | undefined {
  const c = cols ?? [];
  if (!c.length) return undefined;
  return `columns: ${c.slice(0, 8).join(", ")}${c.length > 8 ? ` +${c.length - 8} more` : ""}`;
}

function improvise(view: CaseView): StepDetail | null {
  const nv = view.nodes.improvise;
  const what = "When no known cause fits, the AI writes its own read-only SQL. The database opens read-only and only this merchant's rows are visible.";
  if (nv?.status === "skip") {
    return { by: "AI · not used", ai: true, what, groups: [{ lines: [{ label: "Skipped", text: cap(nv.summary || "not needed") }] }] };
  }
  if (!view.probes.length && !view.llm.improvise) return null;
  const lines = view.probes.map(
    (p): DetailLine => ({
      text: `${p.step}. ${p.purpose}`,
      code: p.sql.trim(),
      tag: p.ok ? undefined : { text: "Rejected", tone: "bad" },
      right: p.ok ? plural(p.rows?.length ?? 0, "row") : undefined,
      sub: p.ok ? columnsLine(p.columns) : (p.error ?? undefined),
    }),
  );
  const groups: DetailGroup[] = [];
  if (lines.length) groups.push({ title: `Queries (${lines.length})`, lines });
  const finding = nv?.summary?.split(" · ").slice(1).join(" · ");
  if (finding) groups.push({ lines: [{ label: "Finding", text: finding.length >= 90 ? `${finding}…` : finding }] });
  return { by: aiBy(view, "improvise"), ai: true, what, groups, raw: aiRaw(view, "improvise") };
}

function verdict(view: CaseView): StepDetail | null {
  const v = view.verdict;
  if (!v) return null;
  const lines: DetailLine[] = [{ label: "Outcome", text: OUTCOME[v.outcome] ?? cap(String(v.outcome).toLowerCase().replace(/_/g, " ")) }];
  const culprits = asArray(v.culprits);
  if (culprits.length) lines.push({ label: "Cause", text: culprits.map(causeTitle).join(" · ") });
  if (v.confidence) lines.push({ label: "Confidence", text: cap(v.confidence.toLowerCase()) });
  if (v.damages_paise) lines.push({ label: "Owed", text: rupees(v.damages_paise), refs: asArray(v.damages_ledger_ids).map((id) => `ledger_entries #${id}`) });
  if (v.headline) lines.push({ label: "In short", text: v.headline });
  const groups: DetailGroup[] = [{ lines }];
  const gun = asArray(v.smoking_gun);
  if (gun.length) {
    const { shown, more } = capped(gun);
    groups.push({ title: `Key evidence (${gun.length})`, lines: shown.map((g): DetailLine => ({ text: g.label, refs: [ref(g)] })), more });
  }
  return { by: CODE, ai: false, what: "Decides from the tested causes. Fixed rules, so the same records always give the same verdict.", groups };
}

function propose(view: CaseView): StepDetail | null {
  const p = view.proposal;
  if (!p) return view.llm.propose ? { by: aiBy(view, "propose"), ai: true, what: PROPOSE_WHAT, groups: [], raw: aiRaw(view, "propose") } : null;
  const lines = asArray(p.actions).map(
    (a): DetailLine => ({
      text: cap(actionShort(a)),
      sub: a.reason,
      refs: [...asArray(a.ledger_entry_ids).map((id) => `ledger_entries #${id}`), a.target_id ? a.target_id : ""].filter(Boolean),
    }),
  );
  const groups: DetailGroup[] = [{ title: `Proposed (${lines.length})`, lines }];
  if (p.rationale) groups.push({ lines: [{ label: "Why", text: p.rationale }] });
  return { by: aiBy(view, "propose"), ai: true, what: PROPOSE_WHAT, groups, raw: aiRaw(view, "propose") };
}
const PROPOSE_WHAT = "The AI suggests a fix. It can't run it: the safety rules check every action next.";

function firewall(view: CaseView): StepDetail | null {
  const fw = view.firewall;
  if (!fw) return null;
  const rank = (s: string) => CHECK_TAG[s]?.rank ?? 5;
  const groups = asArray(fw.actions).map((a): DetailGroup => {
    const fixed = a.decision === "CORRECTED" && a.final ? ` → ${actionShort(a.final)}` : "";
    const d = tagOf(DECISION_TAG, a.decision);
    return {
      title: `${cap(actionShort(a.proposed))}${fixed} · ${d.text}`,
      lines: [...asArray(a.checks)]
        .sort((x, y) => rank(x.status) - rank(y.status))
        .map((c): DetailLine => ({ text: c.label, tag: tagOf(CHECK_TAG, c.status), sub: aiWords(c.detail) })),
    };
  });
  return { by: CODE, ai: false, what: "Fixed rules check each proposed action: ownership, amounts recomputed from the ledger, limits, repeats and tricks.", groups };
}

function execute(view: CaseView): StepDetail | null {
  if (!view.executions.length) return null;
  return {
    by: CODE,
    ai: false,
    what: "Runs what the rules allowed. Anything over the limit waits for a person in Needs you.",
    groups: [
      {
        lines: view.executions.map(
          (e): DetailLine => ({
            text: cap(actionShort({ action_type: e.action_type, amount_paise: e.amount_paise })),
            tag: tagOf(EXEC_TAG, e.status),
            sub: [e.detail, e.approver ? `decided by ${e.approver}` : ""].filter(Boolean).join(" · "),
            refs: [e.action_id ? `action #${e.action_id}` : "", ...asArray(e.ledger_entry_ids).map((id) => `ledger_entries #${id}`)].filter(Boolean),
          }),
        ),
      },
    ],
  };
}

function respond(view: CaseView): StepDetail | null {
  const reply = view.messages.find((m) => m.role === "munim" && m.kind !== "chat" && m.final);
  const s = view.llm.respond;
  if (!reply && !s) return null;
  const lines: DetailLine[] = [];
  if (reply) {
    lines.push({ label: "Language", text: langLabel(reply.language ?? view.intake?.language) });
    lines.push({ label: "Length", text: plural(reply.text.trim().split(/\s+/).filter(Boolean).length, "word") });
    if (reply.en) lines.push({ label: "English copy", text: "Yes, shown under the reply" });
  }
  return {
    by: aiBy(view, "respond"),
    ai: true,
    what: "Writes the reply in the merchant's language from the verdict and what ran. It only uses facts from the case.",
    groups: lines.length ? [{ lines }] : [],
    raw: aiRaw(view, "respond"),
  };
}

function sweep(view: CaseView): StepDetail | null {
  const nv = view.nodes.sweep;
  const who = peopleWord(view.sweep?.suspect_id ?? view.verdict?.culprits?.[0], 2);
  const what = `Looks for the same cause in other ${who}' records. Fixes it finds wait for sign-off.`;
  if (nv?.status === "skip") return { by: CODE, ai: false, what, groups: [{ lines: [{ label: "Skipped", text: cap(nv.summary || "nothing to look for") }] }] };
  const s = view.sweep;
  if (!s) return null;
  const lines: DetailLine[] = [
    { label: "Cause", text: causeTitle(s.suspect_id) },
    { label: cap(who), text: String(s.merchant_count) },
  ];
  if (s.total_paise) lines.push({ label: "Total", text: rupees(s.total_paise) });
  if (s.sweep_id) lines.push({ label: "Batch", text: s.sweep_id, refs: asArray(s.action_ids).length ? [plural(asArray(s.action_ids).length, "action")] : undefined });
  const groups: DetailGroup[] = [{ lines }];
  const victims = asArray(s.victims);
  if (victims.length) {
    const { shown, more } = capped(victims, 6);
    groups.push({
      title: "Merchants found",
      lines: shown.map((v): DetailLine => ({ text: v.business_name, sub: [v.city, v.merchant_id].filter(Boolean).join(" · "), right: rupees(v.damages_paise) })),
      more,
    });
  }
  return { by: CODE, ai: false, what, groups };
}

const BUILDERS: Record<string, (view: CaseView) => StepDetail | null> = { intake, hunch, trail, statements, lineup, improvise, verdict, propose, firewall, execute, respond, sweep };

/** Details for one step row, or null when the step hasn't produced anything to show. */
export function stepDetails(view: CaseView, node: string): StepDetail | null {
  const nv = view.nodes[node];
  if (!nv || nv.status === "pending") return null;
  const b = BUILDERS[node];
  if (!b) return null;
  const d = b(view);
  if (d) return d;
  if (nv.status === "error") return { by: "", ai: false, what: "This step failed.", groups: [{ lines: [{ label: "Error", text: nv.summary || "Unknown error", tag: { text: "Error", tone: "bad" } }] }] };
  return null;
}
