// Chat-first shell: sidebar (chats, Needs you) + one page. A chat starts at the greeting, Munim works on the right,
// then answers in the chat; "How Munim decided" opens the proof on demand.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { ChatHeader, type StatusPill } from "./components/ChatHeader";
import { ChatView, type FollowChip, type SweepLine } from "./components/ChatView";
import { Inbox, inboxStatus } from "./components/Inbox";
import { NeedsYou, type CaseApproval, type Decided, type HandedCase, type SweepGroup } from "./components/NeedsYou";
import { Sources } from "./components/Sources";
import type { SweepReviewRow } from "./components/SweepReview";
import { Sidebar, type SideItem } from "./components/Sidebar";
import { StartScreen } from "./components/StartScreen";
import { WorkPanel, type HowTab } from "./components/WorkPanel";
import { asArray, fmtMs, rupees } from "./format";
import {
  actionShort,
  approvalReason,
  brainName,
  cap,
  caseHeadline,
  causeTitle,
  peopleWord,
  effectiveStatus,
  escalationTeam,
  firstName,
  isEnglish,
  phaseText,
  resultCard,
  rowSubtitle,
  statusTone,
} from "./lib/explain";
import { START_CHIPS, followUpLabel, followUps } from "./lib/suggestions";
import { fixtureForScenario } from "./replay";
import { FALLBACK_SCENARIOS } from "./scenarios";
import { elapsedMs, type ChatMessage } from "./state/caseReducer";
import { deriveApprovals, deriveProviders } from "./state/derive";
import { useCaseSession } from "./state/useCaseSession";
import type { ApprovalRow, CaseRow, Health, InboxItem, MerchantInfo, MunimEvent, ProviderInfo, ProviderSet, Scenario } from "./types";

type Backend = "checking" | "online" | "offline" | "skipped";
type Page = "inbox" | "chat" | "needs" | "sources";
interface SweepDone {
  executed: number;
  total: number;
  merchants: number;
  simulated: boolean;
  /** every merchant in the batch is done (a reviewer may approve them in parts) */
  all: boolean;
}

function readParams() {
  const p = new URLSearchParams(window.location.search);
  const replay = (p.get("replay") ?? "").trim();
  const speed = Number(p.get("speed"));
  return { replay: replay || null, speed: Number.isFinite(speed) && speed > 0 ? speed : 1 };
}

function setReplayParam(name: string | null) {
  const url = new URL(window.location.href);
  if (name) url.searchParams.set("replay", name);
  else {
    url.searchParams.delete("replay");
    url.searchParams.delete("speed");
  }
  window.history.replaceState(null, "", url.toString());
}

const DEFAULT_MERCHANT = "M1001";
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const chipLabel = (scenarioId: string | null | undefined) => START_CHIPS.find((c) => c.scenario === scenarioId)?.label ?? "";
const uniq = <T,>(xs: T[]) => [...new Set(xs)];

/** "Gemini + Sarvam" from the reasoner/writer, or "" when nothing is live. */
function brainNames(p: ProviderSet | null | undefined, onlyLive: boolean): string {
  const list = [p?.reasoner, p?.writer].filter((x): x is ProviderInfo => !!x && (!onlyLive || x.live === true));
  return uniq(list.map((x) => brainName(x.provider, x.live))).join(" + ");
}

/** What an approval row is, in words: "₹2,495 refund for Lakshmi Tiffins". */
function describeApproval(row: ApprovalRow | undefined): string {
  if (!row) return "the action";
  const who = row.business_name ?? row.merchant_id;
  const what = row.action_type === "REFUND_DEBITS" ? `${rupees(row.amount_paise ?? 0)} refund` : actionShort({ action_type: row.action_type, amount_paise: row.amount_paise, team: row.proposed?.team });
  return `${what} for ${who}`;
}

export default function App() {
  const { view, mode, replay, replayTurns, playTurn, openLive, ingest, clear, startReplay, replayControls } = useCaseSession();
  const initial = useRef(readParams()).current;

  // Replay via URL never touches the API: the page must run with no backend and no console errors.
  const [backend, setBackend] = useState<Backend>(initial.replay ? "skipped" : "checking");
  const [health, setHealth] = useState<Health | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>(FALLBACK_SCENARIOS);
  const [merchants, setMerchants] = useState<MerchantInfo[]>([]);
  const [cases, setCases] = useState<CaseRow[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRow[]>([]);

  // UI
  // Inbox is the first page; a recording opened by URL goes straight to its chat.
  const [page, setPage] = useState<Page>(initial.replay ? "chat" : "inbox");
  const [inbox, setInbox] = useState<InboxItem[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [howOpen, setHowOpen] = useState(false);
  const [howTab, setHowTab] = useState<HowTab>("steps");
  const [workHidden, setWorkHidden] = useState(false);
  const [merchantId, setMerchantId] = useState(DEFAULT_MERCHANT);
  const [draft, setDraft] = useState("");
  const [starting, setStarting] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [pendingQ, setPendingQ] = useState<string | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Decided[]>([]);
  const [sweepOutcomes, setSweepOutcomes] = useState<Record<string, SweepDone>>({});
  const [sweepPicked, setSweepPicked] = useState<Record<string, string[]>>({}); // recording: merchants already approved
  const [now, setNow] = useState(() => Date.now());

  const live = backend === "online";
  const isReplay = mode === "replay";

  // ── data loading ──
  const loadLive = useCallback(async () => {
    const [sc, me, ca, ap, ib] = await Promise.allSettled([api.scenarios(), api.merchants(), api.cases(), api.approvals(), api.inbox()]);
    if (sc.status === "fulfilled" && Array.isArray(sc.value) && sc.value.length) setScenarios(sc.value);
    if (me.status === "fulfilled" && Array.isArray(me.value)) setMerchants(me.value);
    if (ca.status === "fulfilled" && Array.isArray(ca.value)) setCases(ca.value);
    if (ap.status === "fulfilled" && Array.isArray(ap.value)) setApprovals(ap.value);
    if (ib.status === "fulfilled" && Array.isArray(ib.value)) setInbox(ib.value);
  }, []);

  const checkBackend = useCallback(async () => {
    setBackend("checking");
    try {
      const h = await api.health();
      setHealth(h);
      setBackend("online");
      await loadLive();
    } catch {
      setBackend("offline");
    }
  }, [loadLive]);

  const refreshSide = useCallback(() => {
    if (backend !== "online") return;
    api
      .approvals()
      .then((r) => setApprovals(Array.isArray(r) ? r : []))
      .catch(() => undefined);
    api
      .cases()
      .then((r) => setCases(Array.isArray(r) ? r : []))
      .catch(() => undefined);
    api
      .inbox()
      .then((r) => setInbox(Array.isArray(r) ? r : []))
      .catch(() => undefined);
  }, [backend]);

  useEffect(() => {
    if (initial.replay) startReplay(initial.replay, { speed: initial.speed });
    else checkBackend();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refresh the chat list / approvals when the live case reaches a milestone (debounced).
  const sideKey = `${view.caseId}|${view.done ? 1 : 0}|${view.executions.map((e) => e.status).join(",")}|${view.approvals.length}|${view.sweep?.sweep_id ?? ""}`;
  useEffect(() => {
    if (mode !== "live") return;
    const t = setTimeout(refreshSide, 400);
    return () => clearTimeout(t);
  }, [sideKey, mode, refreshSide]);

  // Space toggles replay play/pause (when not typing).
  useEffect(() => {
    if (!isReplay || !replay) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.code !== "Space" || (t && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName))) return;
      e.preventDefault();
      if (replay.finished) replayControls.restart();
      else if (replay.playing) replayControls.pause();
      else replayControls.play();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isReplay, replay, replayControls]);

  // ── status ──
  const status = view.caseId ? effectiveStatus(view) : starting ? "INVESTIGATING" : null;
  const running = status === "INVESTIGATING";

  // A live case ticks its clock between events (a slow LLM call shows no events for a while).
  useEffect(() => {
    if (!running || isReplay) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running, isReplay]);

  // ── actions ──
  const resetCaseUi = () => {
    setStarting(null);
    setStartError(null);
    setPendingQ(null);
    setChatError(null);
    setHowOpen(false);
    setWorkHidden(false);
    setDraft("");
  };

  const playFixture = (name: string) => {
    resetCaseUi();
    setOpenId(null);
    setPage("chat");
    setReplayParam(name);
    startReplay(name, { speed: replay?.speed ?? initial.speed });
  };

  const startCase = async (body: { merchant_id: string; complaint_text: string; channel: "chat" | "voice"; scenario_id?: string }) => {
    resetCaseUi();
    setPage("chat");
    setStarting(body.complaint_text);
    setCreating(true);
    try {
      const r = await api.createCase(body);
      setOpenId(r.case_id);
      openLive(r.case_id);
      refreshSide();
    } catch (e) {
      setStarting(null);
      setStartError(`Couldn't start the case: ${errText(e)}`);
      if (!body.scenario_id) setDraft(body.complaint_text);
    } finally {
      setCreating(false);
    }
  };

  const pickScenario = (s: Scenario) => {
    if (!live) {
      const fx = fixtureForScenario(s.id);
      if (fx) playFixture(fx);
      return;
    }
    setMerchantId(s.merchant_id);
    startCase({ merchant_id: s.merchant_id, complaint_text: s.complaint_text, channel: s.channel ?? "chat", scenario_id: s.id });
  };

  const continueItem = (item: InboxItem) => {
    const s = scenarios.find((x) => x.id === item.scenario_id) ?? FALLBACK_SCENARIOS.find((x) => x.id === item.scenario_id);
    if (!live) {
      const fx = fixtureForScenario(item.scenario_id);
      if (fx) playFixture(fx);
      return;
    }
    setMerchantId(item.merchant_id);
    startCase({ merchant_id: item.merchant_id, complaint_text: item.text, channel: s?.channel ?? "chat", scenario_id: item.scenario_id ?? undefined });
  };

  const openCase = (id: string) => {
    setPage("chat");
    if (!live || id === openId) return;
    resetCaseUi();
    setOpenId(id);
    openLive(id);
  };

  const newChat = () => {
    if (isReplay) setReplayParam(null);
    clear();
    resetCaseUi();
    setOpenId(null);
    setPage("chat");
    if (backend === "skipped" || backend === "offline") checkBackend();
  };

  const sendChat = async (text: string) => {
    const id = view.caseId;
    const q = text.trim();
    if (!id || !q || pendingQ) return;
    setChatError(null);
    setPendingQ(q);
    setDraft("");
    try {
      const r = await api.chat(id, q);
      ingest(asArray(r.events));
    } catch (e) {
      // 405 = the server has no chat route: it was started before chat existed and needs a restart.
      setChatError(e instanceof ApiError && e.status === 405 ? "This server is older than the app. Restart it (.\\run.ps1) to turn on chat." : errText(e));
      setDraft(q);
    } finally {
      setPendingQ(null);
    }
  };

  // A teammate sends Munim's draft. Nothing reaches the merchant before this.
  const sendReply = async (m: ChatMessage, text?: string) => {
    const id = view.caseId;
    if (!id) return;
    const lang = m.language ?? null;
    const body = text?.trim() || m.text;
    const edited = !!text && body !== m.text;
    if (!live || isReplay) {
      // Recording: no backend to send through. Show what Send does (no seq, so the recording plays on).
      const ev = { case_id: id, type: "message", at: new Date().toISOString(), data: { role: "munim", kind: "sent", ref: m.seq, text: body, en: edited ? (isEnglish(lang) ? body : null) : (m.en ?? null), language: lang, final: true, by: "you (simulated)", edited } };
      ingest([ev as unknown as MunimEvent]);
      return;
    }
    try {
      const r = await api.sendReply(id, m.seq ?? null, edited ? body : undefined);
      ingest([r.event]);
      refreshSide();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 405 || (e.status === 404 && /^not found$/i.test(e.message)))) throw new Error("This server is older than the app. Restart it (.\\run.ps1) to turn on Send.");
      throw e;
    }
  };

  const decide = (d: Decided) => setDecisions((list) => [d, ...list.filter((x) => x.key !== d.key)]);

  const afterDecision = async (row: ApprovalRow | undefined) => {
    if (view.caseId && mode === "live" && (!row || row.case_id === view.caseId)) {
      try {
        const d = await api.caseDetail(view.caseId);
        ingest(asArray(d.events)); // approval + follow-up message; deduped by seq
      } catch {
        /* the next refresh will catch up */
      }
    }
    refreshSide();
  };

  const approve = async (id: number) => {
    const row = approvals.find((a) => a.action_id === id);
    setBusy(`a${id}`);
    try {
      const r = await api.approve(id);
      const failed = r.result?.status === "FAILED";
      decide({ key: `a${id}`, text: failed ? `Approved, but it failed: ${describeApproval(row)}` : `Approved ${describeApproval(row)}`, bad: failed });
      await afterDecision(row);
    } catch (e) {
      decide({ key: `a${id}`, text: `Couldn't approve ${describeApproval(row)}: ${errText(e)}`, bad: true });
    } finally {
      setBusy(null);
    }
  };

  const reject = async (id: number) => {
    const row = approvals.find((a) => a.action_id === id);
    setBusy(`a${id}`);
    try {
      await api.reject(id);
      decide({ key: `a${id}`, text: `Rejected ${describeApproval(row)}`, bad: true });
      await afterDecision(row);
    } catch (e) {
      decide({ key: `a${id}`, text: `Couldn't reject ${describeApproval(row)}: ${errText(e)}`, bad: true });
    } finally {
      setBusy(null);
    }
  };

  // ── Needs you ──
  const needs = useMemo(() => {
    const caseApprovals: CaseApproval[] = [];
    const sweeps: SweepGroup[] = [];
    const handed: HandedCase[] = [];

    const toApproval = (a: ApprovalRow, scenarioId: string | null | undefined, city: string | null | undefined): CaseApproval => {
      const refund = a.action_type === "REFUND_DEBITS";
      return {
        id: a.action_id,
        caseId: a.case_id ?? null,
        name: a.business_name ?? a.merchant_id,
        sub: [chipLabel(scenarioId), city].filter(Boolean).join(" · "),
        amount: refund ? (a.amount_paise ?? 0) : 0,
        noun: refund ? "refund" : actionShort({ action_type: a.action_type, amount_paise: a.amount_paise, team: a.proposed?.team }),
        reason: approvalReason(a.reasons, a.checks),
      };
    };
    // Why the open case went to another team, in a few words.
    const handedReason = (st: string) => {
      if (st === "BLOCKED") return resultCard(view).lines.find((l) => l.tone === "bad")?.text ?? null;
      return `Sent to the ${escalationTeam(view)} team`;
    };

    if (isReplay) {
      for (const a of deriveApprovals(view)) caseApprovals.push(toApproval(a, view.info?.scenario_id, view.info?.merchant?.city));
      const sw = view.sweep;
      if (sw?.sweep_id && sw.merchant_count > 0 && !sweepOutcomes[sw.sweep_id]?.all) {
        const done = new Set(sweepPicked[sw.sweep_id] ?? []);
        const everyone = asArray(sw.victims);
        const victims = everyone.filter((v) => !done.has(v.merchant_id));
        const left = everyone.length ? victims.length : sw.merchant_count;
        const allActions = asArray(sw.action_ids).length || everyone.length;
        if (left > 0)
          sweeps.push({
            sweepId: sw.sweep_id,
            cause: causeTitle(sw.suspect_id),
            merchants: left,
            who: peopleWord(sw.suspect_id, 1),
            total: done.size ? victims.reduce((t, v) => t + (v.damages_paise ?? 0), 0) : (sw.total_paise ?? 0),
            actions: everyone.length ? Math.round((allActions * victims.length) / everyone.length) : allActions,
            names: uniq(victims.map((v) => v.business_name)),
          });
      }
      const st = view.caseId ? effectiveStatus(view) : "";
      if (view.caseId && view.info && (st === "ESCALATED" || st === "BLOCKED")) {
        handed.push({
          caseId: view.caseId,
          name: view.info.merchant.business_name,
          sub: [chipLabel(view.info.scenario_id), view.info.merchant.city].filter(Boolean).join(" · "),
          pill: st === "BLOCKED" ? "Blocked" : "Sent to a team",
          reason: handedReason(st),
        });
      }
    } else if (live) {
      const byCase = new Map(cases.map((c) => [c.id, c]));
      const groups = new Map<string, ApprovalRow[]>();
      for (const a of approvals) {
        if (a.sweep_id) groups.set(a.sweep_id, [...(groups.get(a.sweep_id) ?? []), a]);
        else caseApprovals.push(toApproval(a, a.case_id ? byCase.get(a.case_id)?.scenario_id : null, a.city));
      }
      for (const [sweepId, rows] of groups) {
        if (sweepOutcomes[sweepId]?.all) continue;
        sweeps.push({
          sweepId,
          cause: causeTitle(rows[0]?.sweep_suspect),
          merchants: new Set(rows.map((r) => r.merchant_id)).size,
          who: peopleWord(rows[0]?.sweep_suspect, 1),
          total: rows.filter((r) => r.action_type === "REFUND_DEBITS").reduce((s, r) => s + (r.amount_paise ?? 0), 0),
          actions: rows.length,
          names: uniq(rows.map((r) => r.business_name ?? r.merchant_id)),
        });
      }
      for (const c of cases) {
        const open = c.id === view.caseId;
        const st = open ? effectiveStatus(view) : c.status;
        if (st !== "ESCALATED" && st !== "BLOCKED") continue;
        handed.push({
          caseId: c.id,
          name: c.business_name ?? c.merchant_id,
          sub: chipLabel(c.scenario_id),
          pill: st === "BLOCKED" ? "Blocked" : "Sent to a team",
          reason: open ? handedReason(st) : null,
        });
      }
    }
    return { caseApprovals, sweeps, handed };
  }, [isReplay, live, view, approvals, cases, sweepOutcomes, sweepPicked]);

  // The batch fix after a person checked each merchant: only the ticked ones are fixed; the rest stay for later.
  const addOutcome = (sweepId: string, add: Omit<SweepDone, "all">, all: boolean) =>
    setSweepOutcomes((o) => {
      const prev = o[sweepId];
      const next = prev
        ? { executed: prev.executed + add.executed, total: prev.total + add.total, merchants: prev.merchants + add.merchants, simulated: prev.simulated || add.simulated, all }
        : { ...add, all };
      return { ...o, [sweepId]: next };
    });

  const loadSweep = async (sweepId: string): Promise<{ rows: SweepReviewRow[]; note: string | null }> => {
    if (!live || isReplay) {
      const sw = view.sweep;
      const done = new Set(sweepPicked[sweepId] ?? []);
      const rows = asArray(sw?.sweep_id === sweepId ? sw.victims : [])
        .filter((v) => !done.has(v.merchant_id))
        .map((v) => ({
          id: v.merchant_id,
          name: v.business_name,
          city: v.city,
          amount: v.damages_paise ?? 0,
          proof: [
            `Same cause: ${causeTitle(sw?.suspect_id)}`,
            ...Object.entries(v.targets ?? {}).map(([k, x]) => `${cap(k.replace(/_id$/, "").replace(/_/g, " "))} ${x}`),
            ...(v.ledger_entry_ids?.length ? [`Debits to pay back: ${v.ledger_entry_ids.map((n) => `#${n}`).join(", ")}`] : []),
          ],
          ledger: [],
        }));
      return { rows, note: "Recording: full ledger rows need the live backend" };
    }
    const d = await api.sweepDetail(sweepId);
    const rows = asArray(d.merchants)
      .filter((m) => m.status === "PENDING_APPROVAL")
      .map((m) => ({
        id: m.merchant_id,
        name: m.business_name,
        city: m.city ?? "",
        amount: m.amount_paise ?? 0,
        proof: [`Same cause: ${causeTitle(d.suspect_id)}`, ...asArray(m.evidence).map((e) => e.label).filter(Boolean)],
        ledger: asArray(m.ledger).map((l) => ({ date: l.value_date, ref: l.txn_ref, amount: l.amount_paise, note: l.narration })),
      }));
    return { rows, note: d.source_case ? `Found from ${d.source_case.business_name}'s case ${d.source_case.id}` : null };
  };

  const approveSweep = async (sweepId: string, merchantIds: string[]) => {
    const g = needs.sweeps.find((s) => s.sweepId === sweepId);
    if (!g || merchantIds.length === 0) return;
    const all = merchantIds.length >= g.merchants;
    const w = g.who;
    const who = all ? `${merchantIds.length === 1 ? "the last" : `all ${merchantIds.length}`} ${merchantIds.length === 1 ? w : `${w}s`}` : `${merchantIds.length} of ${g.merchants} ${w}s`;
    if (!live || isReplay) {
      // Recording: no backend to execute against. Show what approval does, clearly labelled as simulated.
      const victims = asArray(view.sweep?.victims).filter((v) => merchantIds.includes(v.merchant_id));
      const total = victims.reduce((t, v) => t + (v.damages_paise ?? 0), 0);
      setSweepPicked((p) => ({ ...p, [sweepId]: [...(p[sweepId] ?? []), ...merchantIds] }));
      addOutcome(sweepId, { executed: merchantIds.length, total, merchants: merchantIds.length, simulated: true }, all);
      decide({ key: `s${sweepId}-${Date.now()}`, text: `Approved the fix for ${who} · ${rupees(total)} refunded (simulated)`, bad: false });
      return;
    }
    setBusy(`s${sweepId}`);
    try {
      const r = await api.approveSweep(sweepId, all ? undefined : merchantIds);
      const executed = r.executed ?? 0;
      addOutcome(sweepId, { executed, total: r.total_paise ?? 0, merchants: merchantIds.length, simulated: false }, all);
      decide({ key: `s${sweepId}-${Date.now()}`, text: `Approved the fix for ${who} · ${rupees(r.total_paise ?? 0)} refunded`, bad: false });
      refreshSide();
    } catch (e) {
      decide({ key: `s${sweepId}-${Date.now()}`, text: `Batch fix failed: ${errText(e)}`, bad: true });
      throw e;
    } finally {
      setBusy(null);
    }
  };

  const onReset = async () => {
    try {
      await api.reset();
      clear();
      resetCaseUi();
      setOpenId(null);
      setPage("chat");
      setDecisions([]);
      setSweepOutcomes({});
      setSweepPicked({});
      await loadLive();
    } catch (e) {
      setStartError(`Reset failed: ${errText(e)}`);
    }
  };

  const exitReplay = () => {
    setReplayParam(null);
    clear();
    resetCaseUi();
    setOpenId(null);
    setPage("chat");
    checkBackend();
  };

  // ── derived ──
  const demoMerchants = useMemo(() => {
    const list = scenarios.map(
      (s) => s.merchant ?? merchants.find((m) => m.id === s.merchant_id) ?? FALLBACK_SCENARIOS.find((f) => f.id === s.id)?.merchant,
    );
    const seen = new Set<string>();
    return list.filter((m): m is MerchantInfo => !!m && !seen.has(m.id) && !!seen.add(m.id));
  }, [scenarios, merchants]);
  const findMerchant = (id: string | null | undefined) =>
    (id && (demoMerchants.find((m) => m.id === id) ?? merchants.find((m) => m.id === id))) || null;

  const providers: ProviderSet | null = isReplay ? deriveProviders(view) : live ? (health?.providers ?? null) : null;
  const voiceLive = live && !isReplay && health?.providers?.voice?.live === true;
  const showStart = page === "chat" && !isReplay && !openId && !starting;

  // Header merchant: the case's, else the one picked on the start screen (or the recording's, before it loads).
  const replayScenario = isReplay && replay ? FALLBACK_SCENARIOS.find((s) => fixtureForScenario(s.id) === replay.name) : undefined;
  const openRow = openId ? cases.find((c) => c.id === openId) : undefined;
  const headerMerchant: MerchantInfo | null =
    view.info?.merchant ??
    (isReplay
      ? (replayScenario?.merchant ?? null)
      : openRow
        ? (findMerchant(openRow.merchant_id) ?? { id: openRow.merchant_id, business_name: openRow.business_name ?? openRow.merchant_id })
        : (findMerchant(merchantId) ?? demoMerchants[0] ?? null));
  const picker = showStart && live && demoMerchants.length > 1 ? { options: demoMerchants, value: merchantId, onPick: setMerchantId } : null;

  // Status pill (click: show/hide the work panel).
  const liveElapsed = !isReplay && running && view.startedAt ? Math.max(0, now - Date.parse(view.startedAt)) : null;
  const elapsed = Math.max(elapsedMs(view) ?? 0, Number.isFinite(liveElapsed) ? (liveElapsed ?? 0) : 0);
  let pill: StatusPill | null = null;
  if (!showStart && status) {
    switch (status) {
      case "INVESTIGATING":
        pill = { tone: "run", text: elapsed ? `Working · ${fmtMs(elapsed)}` : "Working…", title: workHidden ? "Show the steps" : "Hide the steps" };
        break;
      case "RESOLVED":
        pill = { tone: "ok", text: view.done?.ms ? `Resolved · ${fmtMs(view.done.ms)}` : "Resolved", title: "How Munim decided" };
        break;
      case "AWAITING_APPROVAL":
        pill = { tone: "hum", text: "Waiting for approval", title: "How Munim decided" };
        break;
      case "ESCALATED":
        pill = { tone: "esc", text: "Sent to a team", title: "How Munim decided" };
        break;
      default:
        pill = { tone: statusTone(status), text: cap(status.toLowerCase().replace(/_/g, " ")), title: "How Munim decided" };
    }
  }
  const togglePanel = () => {
    if (running) setWorkHidden((h) => !h);
    else setHowOpen((o) => !o);
  };
  const openHow = () => {
    setHowOpen(true);
  };

  const panelOpen = page === "chat" && !showStart && (running ? !workHidden : howOpen && !!view.caseId);
  const typingText = running ? (view.caseId ? phaseText(view) : "Reading the complaint…") : null;

  // Follow-up chips.
  const customerWord = view.info?.merchant?.category === "passenger" ? "Passenger" : "Merchant";
  const lastMsg = view.messages[view.messages.length - 1];
  const awaitingAnswer = !!pendingQ || lastMsg?.role === "team";
  let chips: FollowChip[] = [];
  if (view.done && !awaitingAnswer) {
    const lang = view.intake?.language ?? view.info?.merchant?.language ?? null;
    if (isReplay) {
      chips = replayTurns
        .filter((t) => !t.used)
        .map((t) => ({
          key: `t${t.index}`,
          label: followUpLabel(t.question, t.en, t.language, customerWord),
          language: isEnglish(t.language) ? null : t.language,
          onClick: () => playTurn(t.index),
        }));
    } else if (live) {
      const asked = view.messages.filter((m) => m.role === "team").map((m) => m.text);
      chips = followUps(view.info?.scenario_id, lang, asked, { whenMoney: status !== "BLOCKED", who: customerWord }).map((f) => ({
        key: f.text,
        label: f.label,
        language: f.language,
        onClick: () => sendChat(f.text),
      }));
    }
  }

  const unusedTurns = replayTurns.some((t) => !t.used);
  const composer = running
    ? { placeholder: "Munim is working…", disabled: true, busy: false, note: null, noteBad: false }
    : isReplay
      ? {
          placeholder: "Ask about this case, or paste the merchant's words…",
          disabled: true,
          busy: false,
          note: view.done ? (unusedTurns ? "This is a recording, so chat needs the live backend. Try a suggested question." : "This is a recording, so chat needs the live backend.") : null,
          noteBad: false,
        }
      : { placeholder: "Ask about this case, or paste the merchant's words…", disabled: !live || !view.caseId, busy: !!pendingQ, note: chatError, noteBad: !!chatError };

  const sweepOutcome = view.sweep?.sweep_id ? sweepOutcomes[view.sweep.sweep_id] : undefined;
  const sweepLine: SweepLine | null =
    view.sweep && view.sweep.merchant_count > 0
      ? {
          count: view.sweep.merchant_count,
          who: peopleWord(view.sweep.suspect_id, 1),
          total: view.sweep.total_paise ?? 0,
          fixed: sweepOutcome ? { executed: sweepOutcome.executed, total: sweepOutcome.total, simulated: sweepOutcome.simulated, merchants: sweepOutcome.merchants, all: sweepOutcome.all } : null,
        }
      : null;

  // Sidebar
  let items: SideItem[] = [];
  if (isReplay) {
    if (view.caseId && view.info) items = [{ id: view.caseId, name: view.info.merchant.business_name, sub: caseHeadline(view), tone: statusTone(effectiveStatus(view)) }];
  } else if (live) {
    items = cases.map((c) =>
      c.id === view.caseId
        ? { id: c.id, name: c.business_name ?? c.merchant_id, sub: caseHeadline(view), tone: statusTone(effectiveStatus(view)), app: c.channel === "app" }
        : { id: c.id, name: c.business_name ?? c.merchant_id, sub: rowSubtitle(c), tone: statusTone(c.status), app: c.channel === "app" },
    );
    if (openId && !items.some((i) => i.id === openId)) {
      items.unshift({
        id: openId,
        name: view.info?.merchant?.business_name ?? headerMerchant?.business_name ?? "New chat",
        sub: view.caseId ? caseHeadline(view) : "Working…",
        tone: view.caseId ? statusTone(effectiveStatus(view)) : "run",
      });
    }
  }
  const brain = isReplay
    ? { label: `Replay${brainNames(providers, false) ? ` · ${brainNames(providers, false)}` : ""}`, live: false }
    : live
      ? brainNames(health?.providers, true)
        ? { label: `Live · ${brainNames(health?.providers, true)}`, live: true }
        : { label: "Scripted brain", live: false }
      : backend === "offline"
        ? { label: "Offline", live: false }
        : { label: "Connecting…", live: false };
  const needsCount = needs.caseApprovals.length + needs.sweeps.length;

  let inboxItems: InboxItem[] = inbox;
  if (!live || isReplay) {
    const replyOf = (): InboxItem["reply"] => {
      const mine = view.messages.filter((m) => m.role === "munim" && m.kind !== "chat" && m.final);
      return mine.some((m) => m.draft) ? "draft" : mine.some((m) => m.sent) ? "sent" : null;
    };
    inboxItems = FALLBACK_SCENARIOS.filter((s) => fixtureForScenario(s.id))
      .map((s): InboxItem => {
        const playing = isReplay && !!view.caseId && view.info?.scenario_id === s.id;
        return {
          id: playing ? (view.caseId as string) : `NEW-${s.id}`,
          case_id: playing ? view.caseId : null,
          scenario_id: s.id,
          merchant_id: s.merchant_id,
          business_name: s.merchant?.business_name,
          owner_name: s.merchant?.owner_name,
          city: s.merchant?.city,
          language: s.language,
          summary: s.title,
          text: s.complaint_text,
          via: s.via ?? "ticket",
          via_note: s.via_note,
          received: s.received,
          status: playing ? (view.done ? effectiveStatus(view) : "INVESTIGATING") : "NEW",
          approvals: playing ? needs.caseApprovals.length : 0,
          reply: playing ? replyOf() : null,
        };
      })
      .sort((a, b) => (a.received ?? "").localeCompare(b.received ?? ""));
  }
  const inboxCount = inboxItems.filter((i) => ["new", "draft"].includes(inboxStatus(i).key)).length;

  return (
    <div className="shell">
      <Sidebar
        items={items}
        activeId={isReplay ? view.caseId : openId}
        onOpen={openCase}
        onNewChat={newChat}
        page={page}
        onNeeds={() => {
          setPage("needs");
          refreshSide();
        }}
        onSources={() => setPage("sources")}
        onInbox={() => {
          setPage("inbox");
          refreshSide();
        }}
        inboxCount={inboxCount}
        needsCount={needsCount}
        brain={brain}
        onReset={live && !isReplay ? onReset : null}
        replay={isReplay ? replay : null}
        controls={isReplay ? replayControls : null}
        onExitReplay={exitReplay}
      />
      <div className="page">
        {page === "inbox" ? (
          <Inbox
            items={inboxItems}
            loading={backend === "checking"}
            offline={!live || isReplay}
            activeCaseId={isReplay ? view.caseId : openId}
            busy={creating}
            error={startError}
            onOpenCase={openCase}
            onContinue={continueItem}
          />
        ) : page === "sources" ? (
          <Sources live={live && !isReplay} />
        ) : page === "needs" ? (
          <NeedsYou
            approvals={needs.caseApprovals}
            sweeps={needs.sweeps}
            handed={needs.handed}
            decided={decisions}
            busy={busy}
            replay={isReplay || !live}
            onApprove={approve}
            onReject={reject}
            onApproveSweep={approveSweep}
            loadSweep={loadSweep}
            onOpen={openCase}
          />
        ) : (
          <>
            <ChatHeader merchant={headerMerchant} picker={picker} pill={pill} onPill={togglePanel} />
            <main className={`work${panelOpen ? " open" : ""}`}>
              {showStart ? (
                <StartScreen
                  firstName={firstName(headerMerchant?.owner_name)}
                  draft={draft}
                  onDraft={setDraft}
                  onSend={(text, channel) => startCase({ merchant_id: merchantId, complaint_text: text, channel })}
                  scenarios={scenarios}
                  onChip={pickScenario}
                  backend={backend}
                  onRetry={checkBackend}
                  voice={voiceLive}
                  busy={creating}
                  error={startError}
                />
              ) : (
                <ChatView
                  key={view.caseId ?? openId ?? "new"}
                  view={view}
                  starting={starting}
                  pendingQ={pendingQ}
                  typingText={typingText}
                  draft={draft}
                  onDraft={setDraft}
                  onSend={sendChat}
                  composer={composer}
                  chips={chips}
                  voice={voiceLive}
                  sweep={sweepLine}
                  onOpenHow={openHow}
                  onReview={() => setPage("needs")}
                  onNewChat={newChat}
                  onSendReply={sendReply}
                />
              )}
              {panelOpen && (
                <WorkPanel
                  view={view}
                  providers={providers}
                  mode={running ? "working" : "how"}
                  tab={howTab}
                  onTab={setHowTab}
                  onClose={() => (running ? setWorkHidden(true) : setHowOpen(false))}
                />
              )}
            </main>
          </>
        )}
      </div>
    </div>
  );
}
