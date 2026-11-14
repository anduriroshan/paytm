"""Munim HTTP API: REST + SSE for the live investigation UI. Serves the built frontend at `/`.

Run:  .venv\\Scripts\\python.exe -m uvicorn munim.api.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from munim import config
from munim.db.conn import one, rows, tx
from munim.graph import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("munim.api")

MERCHANT_COLS = runner.MERCHANT_COLS


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.DB_PATH.exists():
        from munim.db.seed import reset_db
        log.info("no database at %s, seeding demo data", config.DB_PATH)
        reset_db()
    yield


app = FastAPI(title="Munim", version="0.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from munim.api.merchant import router as merchant_router  # noqa: E402  (merchant app: /api/me/*)

app.include_router(merchant_router)


# ───────────────────────────── bodies ─────────────────────────────

class NewCase(BaseModel):
    merchant_id: str
    complaint_text: str
    channel: str = "chat"
    scenario_id: str | None = None


class Approver(BaseModel):
    approver: str = "ops.lead"
    reason: str = ""


class ChatBody(BaseModel):
    text: str


class SendBody(BaseModel):
    seq: int | None = None          # which draft (event seq); default: the latest one not sent
    text: str | None = None         # the teammate's edit; default: Munim's draft as written
    by: str = "ops.lead"


class SweepApprove(Approver):
    merchant_ids: list[str] | None = None    # only these merchants; default: every pending one


class TTSBody(BaseModel):
    text: str
    language: str = "hi"


# ───────────────────────────── meta ─────────────────────────────

@app.get("/api/health")
def health():
    from munim.llm import router
    with tx() as conn:
        counts = {"merchants": conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0],
                  "cases": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]}
    return {"ok": True, "today": config.DEMO_TODAY, "providers": router.describe(), "counts": counts}


@app.get("/api/scenarios")
def scenarios():
    from munim.db.scenarios import SCENARIOS
    with tx() as conn:
        out = []
        for s in SCENARIOS:
            d = s.model_dump(mode="json")
            d["merchant"] = runner.merchant_dict(conn, s.merchant_id)
            out.append(d)
    return out


@app.get("/api/sources")
def sources_list():
    """The connected sources (backend/sources/*.yaml): what Munim can reach, when, and which fixes each one carries out.
    Re-read on every call. Header values and API query strings are never returned."""
    from pathlib import Path

    from munim.sources import current, listing
    out = listing(current())
    try:
        out["dir"] = "backend/" + Path(out["dir"]).resolve().relative_to(config.BACKEND_DIR.resolve()).as_posix()
    except ValueError:
        pass
    return out


@app.get("/api/merchants")
def merchants(q: str = Query("", max_length=80)):
    like = f"%{q.strip()}%"
    with tx() as conn:
        return rows(conn, f"""SELECT {MERCHANT_COLS} FROM merchants
                              WHERE id LIKE ? OR business_name LIKE ? OR owner_name LIKE ? OR city LIKE ?
                              ORDER BY CASE WHEN id LIKE 'M1%' THEN 0 ELSE 1 END, id LIMIT 50""",
                    (like, like, like, like))


@app.get("/api/metrics")
def metrics():
    from munim.actions.executor import metrics as m
    with tx() as conn:
        return m(conn)


@app.post("/api/reset")
async def reset():
    from munim.db.seed import reset_db
    for run in list(runner.RUNS.values()):
        if run.task and not run.task.done():
            run.task.cancel()
    runner.RUNS.clear()
    counts = await asyncio.to_thread(reset_db)
    return {"counts": counts}


# ───────────────────────────── cases ─────────────────────────────

@app.post("/api/cases")
async def new_case(body: NewCase):
    text = body.complaint_text.strip()
    if not text:
        raise HTTPException(400, "complaint_text is empty")
    if len(text) > 2000:
        raise HTTPException(400, "complaint_text is too long (max 2000 characters)")
    try:
        case_id = runner.start_case(body.merchant_id, text, body.channel if body.channel in ("chat", "voice") else "chat", body.scenario_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"case_id": case_id}


@app.get("/api/cases")
def list_cases():
    with tx() as conn:
        return rows(conn, """SELECT c.id, c.merchant_id, m.business_name, c.status, c.outcome, c.language,
                                    c.complaint_text, c.created_at, c.scenario_id, c.channel
                             FROM cases c JOIN merchants m ON m.id = c.merchant_id
                             ORDER BY CAST(SUBSTR(c.id, 6) AS INTEGER) DESC LIMIT 100""")


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    with tx() as conn:
        case = one(conn, "SELECT * FROM cases WHERE id=?", (case_id,))
    if not case:
        raise HTTPException(404, "case not found")
    run = runner.load_run(case_id)
    return {"case": case, "events": [e.model_dump(mode="json") for e in (run.events if run else [])]}


@app.get("/api/cases/{case_id}/events")
async def case_events(case_id: str):
    run = runner.load_run(case_id)
    if not run:
        raise HTTPException(404, "case not found")

    async def stream():
        yield "retry: 2000\n\n"
        async for ev in run.subscribe():
            if ev is None:
                yield ": keep-alive\n\n"
                continue
            yield f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(ev.model_dump(mode='json'), ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/api/cases/{case_id}/messages")
async def case_message(case_id: str, body: ChatBody):
    """Follow-up question on a finished case. Answers from the case file only; cannot move money."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text is empty")
    if len(text) > runner.MAX_QUESTION_CHARS:
        raise HTTPException(400, f"text is too long (max {runner.MAX_QUESTION_CHARS} characters)")
    try:
        events = await runner.chat(case_id, text)
    except KeyError:
        raise HTTPException(404, "case not found")
    except runner.ChatRefused as e:
        raise HTTPException(409, str(e))
    return {"events": [e.model_dump(mode="json") for e in events]}


@app.post("/api/cases/{case_id}/replies/send")
def send_reply(case_id: str, body: SendBody = Body(default_factory=SendBody)):
    """A teammate sends Munim's draft reply (as written or edited). Nothing reaches the merchant before this."""
    if body.text is not None and len(body.text) > runner.MAX_REPLY_CHARS:
        raise HTTPException(400, f"reply is too long (max {runner.MAX_REPLY_CHARS} characters)")
    try:
        ev = runner.send_reply(case_id, body.seq, body.text, body.by)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except runner.ChatRefused as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"event": ev.model_dump(mode="json")}


# ───────────────────────────── inbox ─────────────────────────────

@app.get("/api/inbox")
def inbox():
    """Every issue for the support team: cases Munim has worked (with where the reply stands), then complaints that
    came in and nobody has opened yet (the demo scenarios, with how they arrived)."""
    from munim.db.scenarios import SCENARIOS
    by_sc = {s.id: s for s in SCENARIOS}
    with tx() as conn:
        cases = rows(conn, """SELECT c.id, c.merchant_id, m.business_name, m.owner_name, m.city, c.status, c.outcome,
                                     c.language, c.complaint_text, c.created_at, c.scenario_id, c.channel
                              FROM cases c JOIN merchants m ON m.id = c.merchant_id
                              ORDER BY CAST(SUBSTR(c.id, 6) AS INTEGER) DESC LIMIT 100""")
        waiting = {r["case_id"]: r["n"] for r in rows(conn, """SELECT case_id, COUNT(*) AS n FROM actions
                                                               WHERE status = 'PENDING_APPROVAL' AND case_id IS NOT NULL
                                                               GROUP BY case_id""")}
        shops = {s.merchant_id: runner.merchant_dict(conn, s.merchant_id) for s in SCENARIOS}
    items, opened = [], set()
    for c in cases:
        run = runner.load_run(c["id"])
        evs = run.events if run else []
        intake = next((e.data for e in evs if str(e.type) == "intake" and isinstance(e.data, dict)), {})
        sc = by_sc.get(c["scenario_id"] or "")
        if sc:
            opened.add(sc.id)
        via = "app" if c["channel"] == "app" else (sc.via if sc else ("call" if c["channel"] == "voice" else "chat"))
        items.append({
            "id": c["id"], "case_id": c["id"], "scenario_id": c["scenario_id"], "merchant_id": c["merchant_id"],
            "business_name": c["business_name"], "owner_name": c["owner_name"], "city": c["city"],
            "language": c["language"] or intake.get("language") or (sc.language if sc else None),
            "summary": (sc.title if sc else "") or intake.get("summary_en") or c["complaint_text"][:120],
            "text": c["complaint_text"], "via": via, "via_note": sc.via_note if sc else "",
            "received": sc.received if sc else None, "created_at": c["created_at"],
            "status": c["status"], "outcome": c["outcome"], "approvals": waiting.get(c["id"], 0),
            "reply": runner.reply_state(evs)["state"],
        })
    for s in sorted((s for s in SCENARIOS if s.id not in opened), key=lambda s: s.received):
        shop = shops.get(s.merchant_id) or {}
        items.append({
            "id": f"NEW-{s.id}", "case_id": None, "scenario_id": s.id, "merchant_id": s.merchant_id,
            "business_name": shop.get("business_name"), "owner_name": shop.get("owner_name"), "city": shop.get("city"),
            "language": s.language, "summary": s.title, "text": s.complaint_text, "via": s.via, "via_note": s.via_note,
            "received": s.received, "created_at": None, "status": "NEW", "outcome": None, "approvals": 0, "reply": None,
        })
    return items


# ───────────────────────────── approvals + sweeps ─────────────────────────────

@app.get("/api/approvals")
def approvals():
    from munim.actions.executor import pending
    with tx() as conn:
        return pending(conn)


@app.post("/api/approvals/{action_id}/approve")
async def approve_action(action_id: int, body: Approver = Body(default_factory=Approver)):
    from munim.actions.executor import approve
    try:
        with tx() as conn:
            result = approve(conn, action_id, body.approver)
    except LookupError as e:
        raise HTTPException(404, str(e))
    message = await runner.followup_after_approval(action_id, result, body.approver)
    return {"result": result.model_dump(mode="json"), "message": message}


@app.post("/api/approvals/{action_id}/reject")
async def reject_action(action_id: int, body: Approver = Body(default_factory=Approver)):
    from munim.actions.executor import reject
    try:
        with tx() as conn:
            result = reject(conn, action_id, body.approver, body.reason)
    except LookupError as e:
        raise HTTPException(404, str(e))
    run_case_id = None
    with tx() as conn:
        act = one(conn, "SELECT case_id FROM actions WHERE id=?", (action_id,))
        run_case_id = act and act["case_id"]
    if run_case_id and (run := runner.load_run(run_case_id)):
        run.emit("approval", {"action_id": action_id, "status": result.status, "result": result.model_dump(mode="json"), "approver": body.approver})
        runner.persist_events(run)
    return {"result": result.model_dump(mode="json")}


@app.get("/api/sweeps/{sweep_id}")
def sweep_detail(sweep_id: str):
    """Per merchant: actions + checks, the proof, and the ledger rows the refund would reverse. For review before sign-off."""
    from munim.actions.sweep import sweep_detail as detail
    try:
        with tx() as conn:
            return detail(conn, sweep_id)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.post("/api/sweeps/{sweep_id}/approve")
async def approve_sweep(sweep_id: str, body: SweepApprove = Body(default_factory=SweepApprove)):
    from munim.actions.sweep import approve_sweep as do
    if body.merchant_ids is not None and not body.merchant_ids:
        raise HTTPException(400, "pick at least one merchant")
    try:
        with tx() as conn:
            results = do(conn, sweep_id, body.approver, body.merchant_ids)
    except LookupError as e:
        raise HTTPException(404, str(e))
    executed = [r for r in results if r.status == "EXECUTED"]
    return {"results": [r.model_dump(mode="json") for r in results], "executed": len(executed),
            "total_paise": sum(r.amount_paise for r in executed if r.action_type == "REFUND_DEBITS")}


# ───────────────────────────── merchant passbook ─────────────────────────────

@app.get("/api/merchants/{merchant_id}/passbook")
def passbook(merchant_id: str):
    with tx() as conn:
        return rows(conn, """SELECT id, txn_ref, entry_type, direction, amount_paise, status, value_date, narration, refund_of
                             FROM ledger_entries WHERE merchant_id=? ORDER BY value_date DESC, id DESC LIMIT 25""",
                    (merchant_id,))


# ───────────────────────────── voice (stretch) ─────────────────────────────

@app.post("/api/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
    from munim.voice.sarvam import stt
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")
    if len(audio) > 10 * 1024 * 1024:
        raise HTTPException(413, "audio too large")
    out = await stt(audio, file.filename or "audio.webm", file.content_type or "audio/webm")
    if out.get("error"):
        return JSONResponse(out, status_code=502)
    return out


@app.post("/api/voice/tts")
async def voice_tts(body: TTSBody):
    from munim.voice.sarvam import tts
    out = await tts(body.text[:1500], body.language)
    if out.get("error"):
        return JSONResponse(out, status_code=502)
    return out


# ───────────────────────────── frontend ─────────────────────────────

app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets", check_dir=False), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Built React app (frontend/dist). Checked per request, so `npm run build` needs no server restart."""
    if path.startswith("api/"):
        raise HTTPException(404)
    dist = config.FRONTEND_DIST.resolve()
    index = dist / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "Frontend not built. Run `npm run build` in munim/frontend, or use the Vite dev server on :5173."}, 404)
    f = (dist / path).resolve()
    if path and f.is_file() and dist in f.parents:
        return FileResponse(f)
    return FileResponse(index)
