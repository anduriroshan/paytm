"""Runs investigations in the background and fans their events out to SSE subscribers.

Each case gets a CaseRun: an append-only event list (seq 1..n) plus a wake-up signal. Subscribers replay
history then wait for new events. When the run ends, events are persisted to `case_events` so a case can be
re-opened (and replayed) after a server restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from munim import config
from munim.db.conn import audit, now_iso, one, rows, tx
from munim.events import Event, EventType, make_event

log = logging.getLogger("munim.runner")

MERCHANT_COLS = "id, business_name, owner_name, city, language, category"


class CaseRun:
    def __init__(self, case_id: str, events: list[Event] | None = None, done: bool = False):
        self.case_id = case_id
        self.events: list[Event] = list(events or [])
        self.done = done
        self._signal = asyncio.Event()
        self.task: asyncio.Task | None = None

    def emit(self, type_: EventType | str, data: Any) -> Event:
        ev = make_event(len(self.events) + 1, self.case_id, type_, data)
        self.events.append(ev)
        if ev.type == EventType.DONE:
            self.done = True
        sig, self._signal = self._signal, asyncio.Event()
        sig.set()
        return ev

    async def subscribe(self, keepalive_s: float = 15.0) -> AsyncIterator[Event | None]:
        """Yields every event from seq 1, then live ones; yields None as a keep-alive tick; stops after `done`."""
        i = 0
        while True:
            while i < len(self.events):
                ev = self.events[i]
                i += 1
                yield ev
                if ev.type == EventType.DONE:
                    return
            if self.done:
                return
            sig = self._signal
            if i < len(self.events):
                continue
            try:
                await asyncio.wait_for(sig.wait(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                yield None


RUNS: dict[str, CaseRun] = {}


# ───────────────────────────── persistence ─────────────────────────────

def persist_events(run: CaseRun) -> None:
    try:
        with tx() as conn:
            conn.execute("DELETE FROM case_events WHERE case_id=?", (run.case_id,))
            conn.executemany(
                "INSERT INTO case_events(case_id, seq, type, at, data_json) VALUES (?,?,?,?,?)",
                [(e.case_id, e.seq, str(e.type), e.at, json.dumps(e.data, ensure_ascii=False, default=str)) for e in run.events],
            )
    except Exception:
        log.exception("could not persist events for %s", run.case_id)


def load_run(case_id: str) -> CaseRun | None:
    """In-memory run, or one rebuilt from case_events (after a restart)."""
    if case_id in RUNS:
        return RUNS[case_id]
    with tx() as conn:
        case = one(conn, "SELECT id FROM cases WHERE id=?", (case_id,))
        if not case:
            return None
        evs = rows(conn, "SELECT seq, type, at, data_json FROM case_events WHERE case_id=? ORDER BY seq", (case_id,))
    events = [Event(seq=r["seq"], case_id=case_id, type=EventType(r["type"]), at=r["at"], data=json.loads(r["data_json"])) for r in evs]
    run = CaseRun(case_id, events, done=True)
    RUNS[case_id] = run
    return run


# ───────────────────────────── cases ─────────────────────────────

def merchant_dict(conn, merchant_id: str) -> dict[str, Any] | None:
    return one(conn, f"SELECT {MERCHANT_COLS} FROM merchants WHERE id=?", (merchant_id,))


def next_case_id(conn) -> str:
    r = conn.execute("SELECT MAX(CAST(SUBSTR(id, 6) AS INTEGER)) FROM cases WHERE id LIKE 'CASE-%'").fetchone()
    return f"CASE-{(r[0] or 0) + 1:04d}"


def create_case(merchant_id: str, complaint_text: str, channel: str = "chat", scenario_id: str | None = None) -> tuple[str, dict]:
    with tx() as conn:
        merchant = merchant_dict(conn, merchant_id)
        if not merchant:
            raise KeyError(f"unknown merchant {merchant_id}")
        case_id = next_case_id(conn)
        conn.execute(
            "INSERT INTO cases(id, merchant_id, channel, language, complaint_text, scenario_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (case_id, merchant_id, channel, None, complaint_text, scenario_id, "INVESTIGATING", now_iso()),
        )
        audit(conn, case_id, "system", "case_opened", {"merchant_id": merchant_id, "channel": channel, "scenario_id": scenario_id})
    return case_id, merchant


def case_status(state: dict) -> str:
    execs = state.get("executions") or []
    statuses = {e.get("status") for e in execs}
    if "PENDING_APPROVAL" in statuses:
        return "AWAITING_APPROVAL"
    refunded = any(e.get("status") == "EXECUTED" and e.get("action_type") == "REFUND_DEBITS" for e in execs)
    # An escalation next to an executed refund is only cost recovery (e.g. from a bus operator): the customer is done.
    if not refunded and any(e.get("status") == "EXECUTED" and e.get("action_type") == "ESCALATE" for e in execs):
        return "ESCALATED"
    if any(e.get("status") == "EXECUTED" and e.get("action_type") != "NO_ACTION" for e in execs):
        return "RESOLVED"
    if (state.get("firewall") or {}).get("decision") == "BLOCK":
        return "BLOCKED"
    return "RESOLVED"


async def run_case(case_id: str, merchant: dict, complaint_text: str, channel: str, scenario_id: str | None) -> dict:
    """Run the graph for a case, streaming into RUNS[case_id]. Returns the final state."""
    from .build import get_graph

    run = RUNS.get(case_id) or CaseRun(case_id)
    RUNS[case_id] = run
    t0 = time.perf_counter()
    run.emit(EventType.CASE, {"case_id": case_id, "merchant": merchant, "complaint_text": complaint_text,
                              "channel": channel, "scenario_id": scenario_id})
    run.emit(EventType.MESSAGE, {"role": "merchant", "text": complaint_text, "language": merchant.get("language"), "final": True})

    init = {"case_id": case_id, "merchant": merchant, "complaint_text": complaint_text, "channel": channel,
            "origin": "merchant" if channel == "app" else "team", "scenario_id": scenario_id, "today": config.DEMO_TODAY}
    state: dict[str, Any] = dict(init)
    status = "FAILED"
    try:
        async for mode, chunk in get_graph().astream(init, stream_mode=["custom", "values"]):
            if mode == "custom":
                run.emit(chunk["type"], chunk["data"])
            else:
                state = chunk
        status = case_status(state)
    except Exception as e:  # noqa: BLE001
        log.exception("case %s failed", case_id)
        run.emit(EventType.ERROR, {"node": "graph", "message": f"{type(e).__name__}: {e}"})

    verdict = state.get("verdict") or {}
    try:
        with tx() as conn:
            conn.execute(
                "UPDATE cases SET status=?, language=?, outcome=?, verdict_json=?, reply_text=?, closed_at=? WHERE id=?",
                (status, (state.get("intake") or {}).get("language"), verdict.get("outcome"),
                 json.dumps(verdict, ensure_ascii=False) if verdict else None, state.get("reply"),
                 now_iso() if status in ("RESOLVED", "BLOCKED", "ESCALATED") else None, case_id),
            )
            audit(conn, case_id, "system", "case_closed" if status != "AWAITING_APPROVAL" else "case_awaiting_approval",
                  {"status": status, "outcome": verdict.get("outcome")})
    except Exception:
        log.exception("could not update case %s", case_id)

    run.emit(EventType.DONE, {"case_id": case_id, "status": status, "outcome": verdict.get("outcome"),
                              "decision": (state.get("firewall") or {}).get("decision"),
                              "ms": int((time.perf_counter() - t0) * 1000)})
    persist_events(run)
    return state


def start_case(merchant_id: str, complaint_text: str, channel: str = "chat", scenario_id: str | None = None) -> str:
    case_id, merchant = create_case(merchant_id, complaint_text, channel, scenario_id)
    run = CaseRun(case_id)
    RUNS[case_id] = run
    run.task = asyncio.get_running_loop().create_task(run_case(case_id, merchant, complaint_text, channel, scenario_id))
    return case_id


# ───────────────────────────── approvals → follow-up message ─────────────────────────────

async def followup_after_approval(action_id: int, result: Any, approver: str) -> dict | None:
    """After a human approves/rejects a queued action: append `approval` + a vernacular follow-up `message`
    to the case stream. Returns {text, language} or None for sweep actions without a case."""
    from munim.llm import router
    with tx() as conn:
        act = one(conn, "SELECT case_id, merchant_id FROM actions WHERE id=?", (action_id,))
        if not act or not act["case_id"]:
            return None
        merchant = merchant_dict(conn, act["merchant_id"])
        case = one(conn, "SELECT language, channel FROM cases WHERE id=?", (act["case_id"],))
    case_id = act["case_id"]
    run = load_run(case_id)
    rdata = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    if run:
        run.emit(EventType.APPROVAL, {"action_id": action_id, "status": rdata.get("status"), "result": rdata, "approver": approver})
    language = (case or {}).get("language") or (merchant or {}).get("language") or "en"
    ctx = {"merchant": merchant, "language": language, "case_id": case_id, "execution": rdata,
           "approver": approver, "today": config.DEMO_TODAY}

    def on_delta(kind: str, text: str) -> None:
        if run and text:
            run.emit(EventType.LLM, {"node": "respond", "role": "writer", "provider": "", "model": "", "kind": kind, "delta": text})

    res = await router.generate("writer", "followup", ctx, on_delta=on_delta)
    text = res.text.strip()
    en = await router.english_copy(text, language, router.mock_english("followup", ctx), live_source=res.live)
    if run:
        run.emit(EventType.LLM_DONE, {"node": "respond", "role": "writer", "provider": res.provider, "model": res.model,
                                      "ms": res.ms, "text": text, "usage": res.usage, "live": res.live, "fallback": res.fallback})
        msg = {"role": "munim", "text": text, "en": en, "language": language, "final": True, "kind": "update"}
        if (case or {}).get("channel") != "app":     # team cases: a draft until a teammate presses Send
            msg["draft"] = True
        run.emit(EventType.MESSAGE, msg)
        persist_events(run)
    return {"text": text, "language": language}


# ───────────────────────────── replies: Munim drafts, a teammate sends ─────────────────────────────

MAX_REPLY_CHARS = 2000


def _is_draft(d: dict) -> bool:
    return d.get("role") == "munim" and bool(d.get("draft")) and bool(d.get("final")) and bool(d.get("text"))


def reply_state(events: list[Event]) -> dict[str, Any]:
    """Where the merchant replies on a case stand: drafts not yet sent, and how many were sent."""
    drafts: list[int] = []
    sent: set[int] = set()
    for e in events:
        d = e.data if isinstance(e.data, dict) else {}
        if str(e.type) != "message":
            continue
        if _is_draft(d):
            drafts.append(e.seq)
        elif d.get("kind") == "sent" and isinstance(d.get("ref"), int):
            sent.add(d["ref"])
    waiting = [s for s in drafts if s not in sent]
    return {"waiting": waiting, "sent": len(sent), "state": "draft" if waiting else ("sent" if sent else None)}


def send_reply(case_id: str, seq: int | None = None, text: str | None = None, by: str = "ops.lead") -> Event:
    """A teammate sends Munim's draft (as written, or edited) to the merchant. Appends a `sent` message that points
    at the draft (`ref`). Nothing reaches the merchant before this."""
    run = load_run(case_id)
    if run is None:
        raise KeyError(f"unknown case {case_id}")
    if not run.done:
        raise ChatRefused("Munim is still working on this case")
    st = reply_state(run.events)
    if seq is None:
        if not st["waiting"]:
            raise ChatRefused("no reply waiting to be sent")
        seq = st["waiting"][-1]
    draft = next((e for e in run.events if e.seq == seq and str(e.type) == "message" and _is_draft(e.data)), None)
    if draft is None:
        raise KeyError(f"no draft reply {seq} on {case_id}")
    if seq not in st["waiting"]:
        raise ChatRefused("this reply was already sent")
    d = draft.data
    body = (text if text is not None else d["text"]).strip()[:MAX_REPLY_CHARS]
    if not body:
        raise ValueError("reply is empty")
    edited = body != d["text"].strip()
    lang = d.get("language") or "en"
    by = (str(by or "").strip() or "ops.lead")[:64]
    ev = run.emit(EventType.MESSAGE, {
        "role": "munim", "kind": "sent", "ref": seq, "reply_kind": d.get("kind") or "reply", "text": body,
        "en": (body if lang == "en" else None) if edited else d.get("en"), "language": lang, "final": True,
        "by": by, "edited": edited})
    persist_events(run)
    try:
        with tx() as conn:
            conn.execute("UPDATE cases SET reply_text=? WHERE id=?", (body, case_id))
            if case := one(conn, "SELECT merchant_id FROM cases WHERE id=?", (case_id,)):   # shows in the shop's app
                conn.execute("INSERT INTO notifications(merchant_id, case_id, channel, template, body, sent_at) "
                             "VALUES (?,?,?,?,?,?)", (case["merchant_id"], case_id, "app", "case_reply", body, now_iso()))
            audit(conn, case_id, "human", "reply_sent", {"ref": seq, "by": by, "edited": edited, "language": lang})
    except Exception:
        log.exception("could not record sent reply for %s", case_id)
    return ev


# ───────────────────────────── chat: follow-up questions on a finished case ─────────────────────────────

MAX_CHAT_TURNS = 12
MAX_QUESTION_CHARS = 500
_CHAT_LOCKS: dict[str, asyncio.Lock] = {}


class ChatRefused(Exception):
    """The case can't take a question right now (still running, or too many questions)."""


def case_context(run: CaseRun) -> dict[str, Any]:
    """Everything the case file says, rebuilt from its events. Read-only: this is what chat answers from."""
    ctx: dict[str, Any] = {"case_id": run.case_id, "today": config.DEMO_TODAY, "history": [], "suspects": []}
    execs: list[dict] = []
    drafts: dict[int, dict] = {}
    for ev in run.events:
        t, d = str(ev.type), ev.data if isinstance(ev.data, dict) else {}
        if t == "message" and d.get("kind") == "sent":      # a teammate sent a draft: mark it, don't repeat it
            if (h := drafts.get(d.get("ref"))) is not None:
                h.update(status="sent", text=d.get("text") or h["text"], en=d.get("en") or ("" if d.get("edited") else h["en"]))
            continue
        if t == "case":
            ctx.update(merchant=d.get("merchant") or {}, complaint_text=d.get("complaint_text"), scenario_id=d.get("scenario_id"))
        elif t in ("intake", "trail", "sources", "verdict", "firewall", "sweep"):
            ctx[t] = d
        elif t == "proposal":
            ctx["proposal"] = d
        elif t == "statements":
            ctx["statements"] = d.get("checks") or []
        elif t == "suspect":
            ctx["suspects"].append(d)
        elif t == "execution":
            execs = [dict(r) for r in (d.get("results") or []) if isinstance(r, dict)]
        elif t == "approval":
            res = d.get("result") if isinstance(d.get("result"), dict) else {}
            for e in execs:
                if e.get("action_id") == d.get("action_id"):
                    e["status"] = d.get("status") or res.get("status") or e.get("status")
                    e["detail"] = res.get("detail") or e.get("detail")
        elif t == "message" and d.get("final") and d.get("text"):
            h = {"role": d.get("role"), "text": d.get("text"), "en": d.get("en") or ""}
            if d.get("draft"):
                h["status"] = "draft, not sent yet"
                drafts[ev.seq] = h
            ctx["history"].append(h)
        elif t == "done":
            ctx["status"] = d.get("status")
    ctx["executions"] = execs
    ctx["language"] = (ctx.get("intake") or {}).get("language") or (ctx.get("merchant") or {}).get("language") or "en"
    return ctx


def _is_question(d: dict) -> bool:
    """A chat question on a case: asked by the support teammate (older recordings said role 'merchant')."""
    return d.get("kind") == "chat" and d.get("role") != "munim"


async def chat(case_id: str, text: str) -> list[Event]:
    """Answer a support teammate's question from the case file. The teammate may paste the merchant's own words.
    Appends a `team` question and a munim answer in English (plus a draft reply in the merchant's language when the
    question is theirs) and returns the new events. It never moves money: no executor, no firewall here."""
    from munim.llm import router
    from munim.llm.mock import detect_language
    from munim.models import ChatAnswer

    run = load_run(case_id)
    if run is None:
        raise KeyError(f"unknown case {case_id}")
    if not run.done:
        raise ChatRefused("Munim is still working on this case")
    question = (text or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        raise ValueError("empty question")
    lock = _CHAT_LOCKS.setdefault(case_id, asyncio.Lock())
    async with lock:
        turns = sum(1 for e in run.events if str(e.type) == "message" and isinstance(e.data, dict) and _is_question(e.data))
        if turns >= MAX_CHAT_TURNS:
            raise ChatRefused("too many questions on this case; please start a new chat")
        start = len(run.events)
        q_lang = detect_language(question)
        ctx = case_context(run)
        ctx.update(question=question, question_language=q_lang)
        ans, res = await router.generate_json("reasoner", "chat", ctx, ChatAnswer)
        q_en = question if q_lang == "en" else ans.question_en.strip()
        answer = ans.answer.strip()
        reply_text = ans.reply.strip()
        run.emit(EventType.MESSAGE, {"role": "team", "text": question, "en": q_en, "language": q_lang,
                                     "final": True, "kind": "chat"})
        run.emit(EventType.LLM_DONE, {"node": "chat", "role": "reasoner", "provider": res.provider, "model": res.model,
                                      "ms": res.ms, "text": res.text, "usage": res.usage, "live": res.live,
                                      "fallback": res.fallback})
        msg: dict[str, Any] = {"role": "munim", "text": answer, "en": answer, "language": "en", "final": True, "kind": "chat",
                               "sources": [s for s in ans.sources if s][:3], "new_case": ans.new_case}
        if reply_text and reply_text != answer:
            msg["reply"] = {"text": reply_text, "language": q_lang if q_lang != "en" else "en"}
        run.emit(EventType.MESSAGE, msg)
        persist_events(run)
        try:
            with tx() as conn:
                audit(conn, case_id, "llm", "chat_answer", {"question": question, "answer": ans.model_dump(mode="json"),
                                                             "provider": res.provider, "live": res.live})
        except Exception:
            log.exception("could not audit chat for %s", case_id)
        return run.events[start:]
