"""The investigation's LangGraph nodes. Each node is one step of the detective method and streams
events to the UI through LangGraph's custom stream writer (stream_mode="custom").

Agents (UI swimlanes):  Intake Agent (LLM) · Detective (code + Prism + guarded SQL) · Resolver Agent (LLM, no authority)
                        · Action Firewall (code) · Executor (code) · Communicator Agent (LLM, vernacular)
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Awaitable, Callable

from langgraph.config import get_stream_writer

from munim import config
from munim.db.conn import audit, tx
from munim.events import NODES
from munim.models import (
    ActionType, ExecutionResult, FirewallResult, Hunch, IntakeResult, Probe, ProbePlan, Proposal,
    SourcesReport, StatementCheck, SuspectResult, Trail, Verdict, rupees,
)

from .state import CaseState

AGENT = {n: a for n, a, _ in NODES}

ACTION_CATALOGUE = [
    {"type": ActionType.REFUND_DEBITS, "description": "Credit back specific DEBIT ledger entries (rent, EDI, ticket fare). Needs ledger_entry_ids and amount_paise."},
    {"type": ActionType.CANCEL_SUBSCRIPTION, "description": "Stop future billing. target_id = subscription id."},
    {"type": ActionType.SCHEDULE_PICKUP, "description": "Book a new device pickup. target_id = device id."},
    {"type": ActionType.SEND_KYC_LINK, "description": "Send the merchant a re-KYC / document upload link. target_id = hold id."},
    {"type": ActionType.RELEASE_HOLD, "description": "Release a settlement hold. target_id = hold id."},
    {"type": ActionType.ESCALATE, "description": "Hand the case file to a human team. team = risk | billing | logistics | payments | lending | travel."},
    {"type": ActionType.NO_ACTION, "description": "Nothing to do; explain to the merchant."},
]
MONEY_ACTIONS = {ActionType.REFUND_DEBITS, ActionType.RELEASE_HOLD}

Writer = Callable[[dict[str, Any]], None]


# ───────────────────────────── plumbing ─────────────────────────────

def emit(w: Writer, type_: str, data: Any) -> None:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    w({"type": type_, "data": data})


def node_status(w: Writer, node: str, status: str, ms: int | None = None, summary: str | None = None) -> None:
    d: dict[str, Any] = {"node": node, "agent": AGENT.get(node, ""), "status": status}
    if ms is not None:
        d["ms"] = ms
    if summary:
        d["summary"] = summary
    emit(w, "node", d)


async def pace(mult: float = 1.0) -> None:
    """Cosmetic pacing so evidence appears one item at a time on stage (0 in tests)."""
    d = config.MOCK_STREAM_DELAY_MS
    if d > 0:
        await asyncio.sleep(d * mult / 1000)


def graph_node(name: str, *, soft: bool = False):
    """Wrap a node: start/end/error events with timing. The wrapped fn returns (state_update, summary).
    soft=True: an exception is reported and the investigation continues without this step."""
    def deco(fn: Callable[[CaseState, Writer], Awaitable[tuple[dict, str]]]):
        @functools.wraps(fn)
        async def wrapper(state: CaseState) -> dict:
            w = get_stream_writer()
            node_status(w, name, "start")
            t0 = time.perf_counter()
            try:
                update, summary = await fn(state, w)
            except Exception as e:  # noqa: BLE001 - report, then decide
                ms = int((time.perf_counter() - t0) * 1000)
                msg = f"{type(e).__name__}: {e}"
                emit(w, "error", {"node": name, "message": msg})
                node_status(w, name, "error", ms, msg[:160])
                if soft:
                    return {}
                raise
            node_status(w, name, "end", int((time.perf_counter() - t0) * 1000), summary)
            return update
        return wrapper
    return deco


def llm_streamer(w: Writer, node: str, role: str) -> Callable[[str, str], None]:
    from munim.llm import router
    info = router.describe().get(role, {})
    # A role that is not live (FORCE_MOCK, no key) is answered by the scripted brain, so say so from the
    # first token; otherwise the stream would name the configured provider and llm_done would then say mock.
    if info.get("live"):
        provider, model = info.get("provider", "mock"), info.get("model", "mock")
    else:
        provider, model = "mock", router.MOCK_MODEL

    def on_delta(kind: str, text: str) -> None:
        if text:
            emit(w, "llm", {"node": node, "role": role, "provider": provider, "model": model, "kind": kind, "delta": text})
    return on_delta


def llm_done(w: Writer, node: str, role: str, res: Any) -> None:
    emit(w, "llm_done", {
        "node": node, "role": role, "provider": res.provider, "model": res.model, "ms": res.ms,
        "text": res.text, "usage": res.usage, "live": res.live, "fallback": res.fallback,
    })


def _case_audit(case_id: str, actor: str, event: str, payload: Any) -> None:
    try:
        with tx() as conn:
            audit(conn, case_id, actor, event, payload)
    except Exception:  # audit must never break the run
        pass


# ───────────────────────────── predicates for conditional edges ─────────────────────────────

def any_suspect_matched(state: CaseState) -> bool:
    return any(s.get("status") == "MATCH" for s in state.get("suspects", []))


def should_sweep(state: CaseState) -> bool:
    v = state.get("verdict") or {}
    if not v.get("sweepable_suspect"):
        return False
    return any(e.get("action_type") == ActionType.REFUND_DEBITS and e.get("status") in ("EXECUTED", "PENDING_APPROVAL")
               for e in state.get("executions", []))


# ───────────────────────────── nodes ─────────────────────────────

@graph_node("intake")
async def intake(state: CaseState, w: Writer):
    from munim.llm import router
    ctx = {
        "complaint_text": state["complaint_text"], "channel": state.get("channel", "chat"),
        "merchant": state["merchant"], "today": state["today"], "scenario_id": state.get("scenario_id"),
    }
    result, res = await router.generate_json("reasoner", "intake", ctx, IntakeResult, on_delta=llm_streamer(w, "intake", "reasoner"))
    llm_done(w, "intake", "reasoner", res)
    emit(w, "intake", result)
    _case_audit(state["case_id"], "llm", "intake", result.model_dump(mode="json"))
    flag = " · ⚠ injection suspected" if result.injection_suspected else ""
    return ({"intake": result.model_dump(mode="json")},
            f"{result.intent} · {len(result.claims)} claims · {result.language}{flag}")


@graph_node("hunch", soft=True)
async def hunch(state: CaseState, w: Writer):
    from munim.forensics.suspects import CATALOGUE, lineup
    from munim.llm import router
    with tx() as conn:
        suspects = [CATALOGUE[sid] for sid in lineup(conn, state["merchant"]["id"])]
    ctx = {
        "complaint_text": state["complaint_text"], "merchant": state["merchant"], "scenario_id": state.get("scenario_id"),
        "suspects": [{"id": s.id, "title": s.title, "description": s.description} for s in suspects],
    }
    h: Hunch = await router.hunch(ctx)
    emit(w, "hunch", h)
    top_p = h.probabilities.get(h.top, 0.0)
    return ({"hunch": h.model_dump(mode="json")},
            f"{h.top} {top_p:.0%} · injection {h.injection_score:.0%} · {h.source}")


@graph_node("trail")
async def trail(state: CaseState, w: Writer):
    from munim.forensics.trail import build_trail
    intake_ = IntakeResult.model_validate(state["intake"])
    with tx() as conn:
        t: Trail = build_trail(conn, state["merchant"]["id"], intake_, state["today"])
    emit(w, "trail", t)
    update: dict[str, Any] = {"trail": t.model_dump(mode="json")}
    summary = f"{len(t.nodes)} linked records · {len(t.disputed)} disputed movements"
    report = await _reach_sources(state, intake_, t)
    if report is not None:
        emit(w, "sources", report)
        update["sources"] = report.model_dump(mode="json")
        for h in report.sources:
            if h.reached and h.kind != "database" and (h.records or h.status != "ok"):
                summary += f" · {h.name}: " + (f"{h.records} rows" if h.status == "ok" else "could not be read")
    return (update, summary)


async def _reach_sources(state: CaseState, intake_: IntakeResult, t: Trail) -> SourcesReport | None:
    """Which connected sources matter for this complaint (backend/sources/), reading the outside ones that match.
    Never breaks the trail: any failure here just means no 'sources' event."""
    try:
        from munim import sources
        report = await sources.reach(sources.current(), intake_, state["complaint_text"], state["merchant"]["id"])
        return sources.count_records(report, (n.table for n in t.nodes))
    except Exception:  # noqa: BLE001
        return None


def allowed_catalogue() -> list[dict[str, str]]:
    """The fix list offered to the Resolver: fixes owned by a turned-off source are left out (the firewall refuses them too)."""
    try:
        from munim.sources import current
        reg = current()
        ok = reg.action_allowed
    except Exception:  # noqa: BLE001 - registry unreadable: the firewall still decides
        ok = lambda _a: True  # noqa: E731
    return [{"type": str(a["type"]), "description": a["description"]} for a in ACTION_CATALOGUE if ok(a["type"])]


@graph_node("statements")
async def statements(state: CaseState, w: Writer):
    from munim.forensics.statements import check_statements
    intake_ = IntakeResult.model_validate(state["intake"])
    t = Trail.model_validate(state["trail"])
    with tx() as conn:
        checks: list[StatementCheck] = check_statements(conn, state["merchant"]["id"], intake_, t, state["today"])
    emit(w, "statements", {"checks": [c.model_dump(mode="json") for c in checks]})
    counts: dict[str, int] = {}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    summary = " · ".join(f"{n} {s.lower().replace('_', ' ')}" for s, n in counts.items()) or "no checkable claims"
    return ({"statements": [c.model_dump(mode="json") for c in checks]}, summary)


@graph_node("lineup")
async def lineup(state: CaseState, w: Writer):
    from munim.forensics.suspects import interrogate
    intake_ = IntakeResult.model_validate(state["intake"])
    t = Trail.model_validate(state["trail"])
    with tx() as conn:
        suspects: list[SuspectResult] = interrogate(conn, state["merchant"]["id"], intake_, t, state["today"])
    for s in suspects:
        await pace(8)
        emit(w, "suspect", s)
    matched = [s.suspect_id for s in suspects if s.status == "MATCH"]
    elim = sum(1 for s in suspects if s.status == "ELIMINATED")
    summary = (f"MATCH: {', '.join(matched)}" if matched else "no known suspect matches") + f" · {elim} eliminated"
    return ({"suspects": [s.model_dump(mode="json") for s in suspects]}, summary)


@graph_node("improvise")
async def improvise(state: CaseState, w: Writer):
    from munim.forensics.probe import run_probe
    from munim.forensics.schema_card import schema_card
    from munim.llm import router
    from munim.sources import current
    report = SourcesReport.model_validate(state["sources"]) if state.get("sources") else None
    card = schema_card(report.sources if report else None)
    tables = current().enabled_tables()
    probes: list[Probe] = []
    plan: ProbePlan | None = None
    max_steps = config.MAX_PROBES
    on_delta = llm_streamer(w, "improvise", "reasoner")
    for step in range(1, max_steps + 2):
        ctx = {
            "complaint_text": state["complaint_text"], "intake": state["intake"], "merchant_id": state["merchant"]["id"],
            "today": state["today"], "schema_card": card, "probes": [p.model_dump(mode="json") for p in probes],
            "step": step, "max_steps": max_steps, "scenario_id": state.get("scenario_id"),
            "sources": state.get("sources"),
        }
        plan, res = await router.generate_json("reasoner", "improvise", ctx, ProbePlan, on_delta=on_delta)
        llm_done(w, "improvise", "reasoner", res)
        if plan.done or not plan.sql:
            break
        if len(probes) >= max_steps:   # budget exhausted: stop here, whatever the model wanted
            plan = ProbePlan(done=True, finding=plan.finding, evidence_ledger_ids=plan.evidence_ledger_ids)
            break
        probe = run_probe(plan.sql, state["merchant"]["id"], step, plan.purpose or f"probe {step}", tables=tables)
        probes.append(probe)
        emit(w, "probe", probe)
        _case_audit(state["case_id"], "detective", "probe", probe.model_dump(mode="json"))
    if plan is None or not plan.done:
        plan = ProbePlan(done=True)
    # evidence ids must come from rows the probes actually returned (the LLM can't invent evidence)
    seen = {str(v) for p in probes for row in p.rows for v in row}
    plan = plan.model_copy(update={"evidence_ledger_ids": [i for i in plan.evidence_ledger_ids if str(i) in seen]})
    summary = f"{len(probes)} read-only probe(s)" + (f" · {plan.finding[:90]}" if plan.finding else " · nothing found")
    return ({"probes": [p.model_dump(mode="json") for p in probes], "plan": plan.model_dump(mode="json")}, summary)


@graph_node("verdict")
async def verdict(state: CaseState, w: Writer):
    from munim.forensics.verdict import decide_verdict
    if state.get("plan") is None:   # improvise was not needed: a catalogued suspect matched
        node_status(w, "improvise", "skip", summary="a known suspect matched; no probes needed")
    suspects = [SuspectResult.model_validate(s) for s in state.get("suspects", [])]
    intake_ = IntakeResult.model_validate(state["intake"])
    plan = ProbePlan.model_validate(state["plan"]) if state.get("plan") else None
    probes = [Probe.model_validate(p) for p in state.get("probes", [])]
    v: Verdict = decide_verdict(suspects, intake_, plan, probes)
    emit(w, "verdict", v)
    _case_audit(state["case_id"], "detective", "verdict", v.model_dump(mode="json"))
    dmg = f" · damages {rupees(v.damages_paise)}" if v.damages_paise else ""
    return ({"verdict": v.model_dump(mode="json")}, f"{v.outcome} ({v.confidence}){dmg}")


@graph_node("propose")
async def propose(state: CaseState, w: Writer):
    from munim.llm import router
    ctx = {
        "complaint_text": state["complaint_text"], "intake": state["intake"], "verdict": state["verdict"],
        "statements": state.get("statements", []), "merchant": state["merchant"], "today": state["today"],
        "action_catalogue": allowed_catalogue(),
        "scenario_id": state.get("scenario_id"),
    }
    p, res = await router.generate_json("reasoner", "propose", ctx, Proposal, on_delta=llm_streamer(w, "propose", "reasoner"))
    llm_done(w, "propose", "reasoner", res)
    emit(w, "proposal", p)
    _case_audit(state["case_id"], "llm", "proposal", p.model_dump(mode="json"))
    parts = [f"{a.action_type}" + (f" {rupees(a.amount_paise)}" if a.amount_paise else "") for a in p.actions]
    return ({"proposal": p.model_dump(mode="json")}, " + ".join(parts) or "no actions")


@graph_node("firewall")
async def firewall(state: CaseState, w: Writer):
    from munim.firewall.policy import evaluate
    with tx() as conn:
        fw: FirewallResult = evaluate(
            conn, merchant_id=state["merchant"]["id"], proposal=Proposal.model_validate(state["proposal"]),
            verdict=Verdict.model_validate(state["verdict"]), intake=IntakeResult.model_validate(state["intake"]),
            hunch=Hunch.model_validate(state["hunch"]) if state.get("hunch") else None,
            case_id=state["case_id"], today=state["today"], origin=state.get("origin") or "team",
        )
    emit(w, "firewall", fw)
    per = " · ".join(f"{a.proposed.action_type}→{a.decision}" for a in fw.actions)
    return ({"firewall": fw.model_dump(mode="json")}, f"{fw.decision}: {per}")


@graph_node("execute")
async def execute(state: CaseState, w: Writer):
    from munim.actions.executor import record_and_execute
    with tx() as conn:
        results: list[ExecutionResult] = record_and_execute(
            conn, case_id=state["case_id"], merchant_id=state["merchant"]["id"],
            firewall=FirewallResult.model_validate(state["firewall"]), verdict=Verdict.model_validate(state["verdict"]),
            today=state["today"],
        )
    emit(w, "execution", {"results": [r.model_dump(mode="json") for r in results]})
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return ({"executions": [r.model_dump(mode="json") for r in results]},
            " · ".join(f"{n} {s.lower().replace('_', ' ')}" for s, n in counts.items()))


@graph_node("respond")
async def respond(state: CaseState, w: Writer):
    from munim.llm import router
    lang = (state.get("intake") or {}).get("language") or state["merchant"].get("language") or "en"
    if state.get("channel") == "app" and lang == "en":    # the merchant app speaks the shop's own language
        lang = state["merchant"].get("language") or "en"
    ctx = {
        "merchant": state["merchant"], "language": lang, "complaint_text": state["complaint_text"],
        "intake": state["intake"], "verdict": state["verdict"], "statements": state.get("statements", []),
        "firewall": state["firewall"], "executions": state.get("executions", []), "case_id": state["case_id"],
        "today": state["today"], "scenario_id": state.get("scenario_id"),
    }
    res = await router.generate("writer", "respond", ctx, on_delta=llm_streamer(w, "respond", "writer"))
    llm_done(w, "respond", "writer", res)
    text = res.text.strip()
    en = await router.english_copy(text, lang, router.mock_english("respond", ctx), live_source=res.live)
    msg = {"role": "munim", "text": text, "en": en, "language": lang, "final": True}
    if state.get("channel") != "app":     # team cases: a draft on the case; a teammate presses Send
        msg["draft"] = True
    emit(w, "message", msg)
    if not should_sweep(state):
        node_status(w, "sweep", "skip", summary="no fleet-wide fault to sweep for")
    return ({"reply": text, "language": lang}, f"{len(text.split())} words · {lang} · {res.provider}")


@graph_node("sweep", soft=True)
async def sweep(state: CaseState, w: Writer):
    from munim.actions.sweep import create_sweep
    from munim.forensics.suspects import find_victims
    from munim.models import SweepResult, SweepVictim
    suspect_id = state["verdict"]["sweepable_suspect"]
    merchant_id = state["merchant"]["id"]
    who = "passengers" if state["merchant"].get("category") == "passenger" else "merchants"
    with tx() as conn:
        existing = conn.execute(
            "SELECT id FROM sweeps WHERE suspect_id=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1", (suspect_id,)
        ).fetchone()
        victims = find_victims(conn, suspect_id, merchant_id, state["today"])
        if not victims:
            return ({"sweep": None}, f"no other {who} affected")
        if existing:   # a sweep for this pattern is already waiting for sign-off: show it, don't queue twice
            action_ids = [r[0] for r in conn.execute(
                "SELECT id FROM actions WHERE sweep_id=? AND status='PENDING_APPROVAL' ORDER BY id", (existing[0],))]
            names = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, business_name, city FROM merchants")}
            vs = [SweepVictim(merchant_id=v.merchant_id or "", business_name=names.get(v.merchant_id, ("", ""))[0],
                              city=names.get(v.merchant_id, ("", ""))[1], damages_paise=v.damages_paise,
                              ledger_entry_ids=v.damages_ledger_ids, targets=v.targets) for v in victims]
            result = SweepResult(sweep_id=existing[0], suspect_id=suspect_id, merchant_count=len(vs),
                                 total_paise=sum(v.damages_paise for v in vs), victims=vs, action_ids=action_ids)
        else:
            result = create_sweep(conn, source_case_id=state["case_id"], suspect_id=suspect_id, victims=victims, today=state["today"])
    if not result.sweep_id or not result.merchant_count:
        return ({"sweep": None}, f"no other {who} qualify for a refund")
    emit(w, "sweep", result)
    return ({"sweep": result.model_dump(mode="json")},
            f"{suspect_id} in {result.merchant_count} more {who} · {rupees(result.total_paise)} queued for sign-off")
