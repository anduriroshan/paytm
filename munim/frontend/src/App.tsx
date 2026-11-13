// Chat-first shell: sidebar (chats, Needs you) + one page. A chat starts at the greeting, Munim works on the right,
// then answers in the chat; "How Munim decided" opens the proof on demand.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { ChatHeader, type StatusPill } from "./components/ChatHeader";
import { ChatView, type FollowChip, type SweepLine } from "./components/ChatView";
import { NeedsYou, type CaseApproval, type Decided, type HandedCase, type SweepGroup } from "./components/NeedsYou";
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
import { elapsedMs } from "./state/caseReducer";
import { deriveApprovals, deriveProviders } from "./state/derive";
import { useCaseSession } from "./state/useCaseSession";
import type { ApprovalRow, CaseRow, Health, MerchantInfo, ProviderInfo, ProviderSet, Scenario } from "./types";

type Backend = "checking" | "online" | "offline" | "skipped";
type Page = "chat" | "needs";
interface SweepDone {
  executed: number;
  total: number;
  merchants: number;
  simulated: boolean;
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
  const [page, setPage] = useState<Page>("chat");
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
  const [now, setNow] = useState(() => Date.now());

  const live = backend === "online";
  const isReplay = mode === "replay";

  // ── data loading ──
  const loadLive = useCallback(async () => {
    const [sc, me, ca, ap] = await Promise.allSettled([api.scenarios(), api.merchants(), api.cases(), api.approvals()]);
    if (sc.status === "fulfilled" && Array.isArray(sc.value) && sc.value.length) setScenarios(sc.value);
    if (me.status === "fulfilled" && Array.isArray(me.value)) setMerchants(me.value);
    if (ca.status === "fulfilled" && Array.isArray(ca.value)) setCases(ca.value);
    if (ap.status === "fulfilled" && Array.isArray(ap.value)) setApprovals(ap.value);
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
      setChatError(e instanceof ApiError && e.status === 405 ? "This server is older than the app. Restart it (.\run.ps1) to turn on chat." : errText(e));
      setDraft(q);
    } finally {
      setPendingQ(null);
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
      if (sw?.sweep_id && sw.merchant_count > 0 && !sweepOutcomes[sw.sweep_id]) {
        const victims = asArray(sw.victims);
        sweeps.push({
          sweepId: sw.sweep_id,
          cause: causeTitle(sw.suspect_id),
          merchants: sw.merchant_count,
          total: sw.total_paise ?? 0,
          actions: asArray(sw.action_ids).length || victims.length,
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
        if (sweepOutcomes[sweepId]) continue;
        sweeps.push({
          sweepId,
          cause: causeTitle(rows[0]?.sweep_suspect),
          merchants: new Set(rows.map((r) => r.merchant_id)).size,
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
  }, [isReplay, live, view, approvals, cases, sweepOutcomes]);

  const approveSweep = async (sweepId: string) => {
    const g = needs.sweeps.find((s) => s.sweepId === sweepId);
    if (!g) return;
    if (!live || isReplay) {
      // Replay: no backend to execute against. Show what approval does, clearly labelled as simulated.
      setSweepOutcomes((o) => ({ ...o, [sweepId]: { executed: g.actions, total: g.total, merchants: g.merchants, simulated: true } }));
      decide({ key: `s${sweepId}`, text: `Approved ${g.actions} actions · ${rupees(g.total)} refunded to ${g.merchants} merchants (simulated)`, bad: false });
      return;
    }
    setBusy(`s${sweepId}`);
    try {
      const r = await api.approveSweep(sweepId);
      const executed = r.executed ?? 0;
      setSweepOutcomes((o) => ({ ...o, [sweepId]: { executed, total: r.total_paise ?? 0, merchants: g.merchants, simulated: false } }));
      decide({ key: `s${sweepId}`, text: `Approved ${executed} actions · ${rupees(r.total_paise ?? 0)} refunded to ${g.merchants} merchants`, bad: false });
      refreshSide();
    } catch (e) {
      decide({ key: `s${sweepId}`, text: `Batch fix failed: ${errText(e)}`, bad: true });
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
          label: followUpLabel(t.question, t.en, t.language),
          language: isEnglish(t.language) ? null : t.language,
          onClick: () => playTurn(t.index),
        }));
    } else if (live) {
      const asked = view.messages.filter((m) => m.role === "team").map((m) => m.text);
      chips = followUps(view.info?.scenario_id, lang, asked, { whenMoney: status !== "BLOCKED" }).map((f) => ({
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
          total: view.sweep.total_paise ?? 0,
          fixed: sweepOutcome ? { executed: sweepOutcome.executed, total: sweepOutcome.total, simulated: sweepOutcome.simulated } : null,
        }
      : null;

  // Sidebar
  let items: SideItem[] = [];
  if (isReplay) {
    if (view.caseId && view.info) items = [{ id: view.caseId, name: view.info.merchant.business_name, sub: caseHeadline(view), tone: statusTone(effectiveStatus(view)) }];
  } else if (live) {
    items = cases.map((c) =>
      c.id === view.caseId
        ? { id: c.id, name: c.business_name ?? c.merchant_id, sub: caseHeadline(view), tone: statusTone(effectiveStatus(view)) }
        : { id: c.id, name: c.business_name ?? c.merchant_id, sub: rowSubtitle(c), tone: statusTone(c.status) },
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
        needsCount={needsCount}
        brain={brain}
        onReset={live && !isReplay ? onReset : null}
        replay={isReplay ? replay : null}
        controls={isReplay ? replayControls : null}
        onExitReplay={exitReplay}
      />
      <div className="page">
        {page === "needs" ? (
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
