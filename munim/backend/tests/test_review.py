"""People stay in charge: Munim's replies are drafts a teammate sends, sweeps are reviewed per merchant, and the
Inbox lists every issue. Offline mock mode (no network, no credits), fictional demo data."""
from __future__ import annotations

import asyncio
import time

import pytest

from munim import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from munim.access import assistant
    from munim.api.main import app
    from munim.db.seed import reset_db
    from munim.graph import runner
    db = tmp_path / "munim_review.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "FORCE_MOCK", True)
    monkeypatch.setattr(config, "MOCK_STREAM_DELAY_MS", 0)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    reset_db(db)
    runner.RUNS.clear()
    runner._CHAT_LOCKS.clear()
    assistant._ASKS.clear()
    with TestClient(app) as c:
        yield c
    runner.RUNS.clear()
    runner._CHAT_LOCKS.clear()


def run_scenario(c, scenario_id: str) -> tuple[str, list[dict]]:
    sc = next(s for s in c.get("/api/scenarios").json() if s["id"] == scenario_id)
    cid = c.post("/api/cases", json={"merchant_id": sc["merchant_id"], "complaint_text": sc["complaint_text"],
                                     "scenario_id": sc["id"]}).json()["case_id"]
    deadline = time.time() + 30
    evs: list[dict] = []
    while time.time() < deadline:
        evs = c.get(f"/api/cases/{cid}").json()["events"]
        if evs and evs[-1]["type"] == "done":
            break
        time.sleep(0.05)
    assert evs and evs[-1]["type"] == "done"
    return cid, evs


def login(c, mid: str) -> dict:
    r = c.post("/api/me/login", json={"merchant_id": mid, "otp": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def replies(evs: list[dict]) -> list[dict]:
    return [e for e in evs if e["type"] == "message" and e["data"].get("role") == "munim" and e["data"].get("kind") != "chat"]


# ───────────────────────────── drafts ─────────────────────────────

def test_team_reply_is_a_draft_until_a_teammate_sends_it(client):
    c = client
    cid, evs = run_scenario(c, "zombie_rent")
    draft = replies(evs)[-1]
    assert draft["data"]["draft"] is True and draft["data"]["language"] == "hi" and draft["data"]["text"]
    me = login(c, "M1001")
    card = c.get(f"/api/me/cases/{cid}", headers=me).json()
    assert card["reply"] is None, "the shop must not see a draft"
    assert all(x["reply"] is None for x in c.get("/api/me/cases", headers=me).json())

    r = c.post(f"/api/cases/{cid}/replies/send", json={"by": "asha"})
    assert r.status_code == 200, r.text
    sent = r.json()["event"]["data"]
    assert sent["kind"] == "sent" and sent["ref"] == draft["seq"] and sent["text"] == draft["data"]["text"]
    assert sent["by"] == "asha" and sent["edited"] is False and sent["en"] == draft["data"]["en"]
    card = c.get(f"/api/me/cases/{cid}", headers=me).json()
    assert card["reply"]["text"] == draft["data"]["text"] and card["reply"]["kind"] == "reply"

    assert c.post(f"/api/cases/{cid}/replies/send", json={}).status_code == 409          # nothing left to send
    assert c.post(f"/api/cases/{cid}/replies/send", json={"seq": draft["seq"]}).status_code == 409   # no double send
    assert c.post("/api/cases/CASE-9999/replies/send", json={}).status_code == 404
    evs = c.get(f"/api/cases/{cid}").json()["events"]
    assert sum(1 for e in replies(evs) if e["data"].get("kind") == "sent") == 1


def test_edited_reply_goes_out_as_edited(client):
    c = client
    cid, evs = run_scenario(c, "legit_rent")
    assert c.post(f"/api/cases/{cid}/replies/send", json={"text": "   "}).status_code == 400
    assert c.post(f"/api/cases/{cid}/replies/send", json={"text": "x" * 2001}).status_code == 400
    r = c.post(f"/api/cases/{cid}/replies/send", json={"text": "नमस्ते, पिकअप कल सुबह 10 बजे है।"}).json()["event"]["data"]
    assert r["edited"] is True and r["en"] is None and r["language"] == "hi"
    card = c.get(f"/api/me/cases/{cid}", headers=login(c, "M1004")).json()
    assert card["reply"]["text"] == "नमस्ते, पिकअप कल सुबह 10 बजे है।"


def test_update_after_approval_is_also_a_draft(client):
    c = client
    cid, evs = run_scenario(c, "double_billing")
    first = replies(evs)[-1]
    pend = [a for a in c.get("/api/approvals").json() if a["case_id"] == cid]
    assert pend
    for a in pend:
        assert c.post(f"/api/approvals/{a['action_id']}/approve", json={"approver": "asha"}).status_code == 200
    evs = c.get(f"/api/cases/{cid}").json()["events"]
    updates = [e for e in replies(evs) if e["data"].get("kind") == "update"]
    assert updates and all(u["data"]["draft"] is True for u in updates)
    me = login(c, "M1002")
    assert c.get(f"/api/me/cases/{cid}", headers=me).json()["reply"] is None
    # send the first reply explicitly by seq, then the update (default = latest waiting)
    assert c.post(f"/api/cases/{cid}/replies/send", json={"seq": first["seq"]}).status_code == 200
    assert c.get(f"/api/me/cases/{cid}", headers=me).json()["reply"]["kind"] == "reply"
    assert c.post(f"/api/cases/{cid}/replies/send", json={}).status_code == 200
    rep = c.get(f"/api/me/cases/{cid}", headers=me).json()["reply"]
    assert rep["kind"] == "update" and rep["text"] == updates[-1]["data"]["text"]


def test_case_file_tells_chat_what_was_sent(client):
    from munim.graph import runner
    c = client
    cid, evs = run_scenario(c, "kyc_hold")
    ctx = runner.case_context(runner.load_run(cid))
    assert [h.get("status") for h in ctx["history"] if h["role"] == "munim"][-1] == "draft, not sent yet"
    c.post(f"/api/cases/{cid}/replies/send", json={})
    ctx = runner.case_context(runner.load_run(cid))
    munim = [h for h in ctx["history"] if h["role"] == "munim"]
    assert munim[-1]["status"] == "sent" and len(munim) == 1, "a sent reply is marked, not repeated"


def test_app_chat_replies_are_not_drafts(client):
    from munim.graph import runner
    c = client
    me = login(c, "M1001")
    r = c.post("/api/me/ask", headers=me, json={"text": "Why was ₹499 cut from my account every month after I returned the Soundbox?"})
    assert r.status_code == 200, r.text
    for run in runner.RUNS.values():
        deadline = time.time() + 30
        while not run.done and time.time() < deadline:
            time.sleep(0.05)
        assert not any(e.data.get("draft") for e in run.events if str(e.type) == "message" and isinstance(e.data, dict))


# ───────────────────────────── sweep review ─────────────────────────────

def test_sweep_detail_shows_each_merchant_with_proof(client):
    c = client
    cid, evs = run_scenario(c, "zombie_rent")
    sw = next(e for e in evs if e["type"] == "sweep")["data"]
    d = c.get(f"/api/sweeps/{sw['sweep_id']}").json()
    assert d["sweep_id"] == sw["sweep_id"] and d["suspect_id"] == sw["suspect_id"] and d["status"] == "PENDING"
    assert d["merchant_count"] == sw["merchant_count"] == d["pending"] and d["total_paise"] == sw["total_paise"]
    assert d["source_case"]["id"] == cid
    for m in d["merchants"]:
        assert m["business_name"] and m["amount_paise"] > 0 and m["status"] == "PENDING_APPROVAL"
        assert m["ledger"] and sum(x["amount_paise"] for x in m["ledger"]) == m["amount_paise"]
        assert all(x["entry_type"] == "RENT_DEBIT" for x in m["ledger"])
        assert any(ch["id"] == "sweep_batch" for a in m["actions"] for ch in a["checks"])
        assert m["merchant_id"] != "M1001"
    assert c.get("/api/sweeps/SWP-9999").status_code == 404


def test_sweep_approves_only_the_picked_merchants(client):
    c = client
    cid, evs = run_scenario(c, "zombie_rent")
    sid = next(e for e in evs if e["type"] == "sweep")["data"]["sweep_id"]
    d = c.get(f"/api/sweeps/{sid}").json()
    picked = [m["merchant_id"] for m in d["merchants"][:2]]
    assert c.post(f"/api/sweeps/{sid}/approve", json={"merchant_ids": []}).status_code == 400
    res = c.post(f"/api/sweeps/{sid}/approve", json={"approver": "asha", "merchant_ids": picked}).json()
    assert res["executed"] >= 2
    picked_ids = {a["action_id"] for m in d["merchants"] if m["merchant_id"] in picked for a in m["actions"]}
    assert {r["action_id"] for r in res["results"]} == picked_ids
    d2 = c.get(f"/api/sweeps/{sid}").json()
    by = {m["merchant_id"]: m for m in d2["merchants"]}
    assert all(by[m]["status"] == "EXECUTED" for m in picked)
    assert d2["pending"] == d["pending"] - 2 and d2["status"] == "PENDING"
    others = [m for m in d2["merchants"] if m["merchant_id"] not in picked]
    assert all(m["status"] == "PENDING_APPROVAL" for m in others)
    left = {a["sweep_id"] for a in c.get("/api/approvals").json() if a["sweep_id"]}
    assert sid in left
    res = c.post(f"/api/sweeps/{sid}/approve", json={}).json()       # the rest
    assert c.get(f"/api/sweeps/{sid}").json()["status"] == "APPROVED" and res["executed"] >= len(others)


# ───────────────────────────── inbox ─────────────────────────────

def test_inbox_lists_new_issues_then_cases_with_reply_state(client):
    c = client
    items = c.get("/api/inbox").json()
    assert len(items) == 11 and all(i["status"] == "NEW" and i["case_id"] is None for i in items)
    assert {i["via"] for i in items} == {"call", "email", "ticket"}
    assert all(i["business_name"] and i["summary"] and i["received"] for i in items)
    assert [i["received"] for i in items] == sorted(i["received"] for i in items)

    cid, _ = run_scenario(c, "kyc_hold")
    items = c.get("/api/inbox").json()
    row = next(i for i in items if i["case_id"] == cid)
    assert row["scenario_id"] == "kyc_hold" and row["via"] == "call" and row["reply"] == "draft"
    assert row["summary"] and row["language"] == "te" and row["received"] == "10:48"
    assert sum(i["status"] == "NEW" for i in items) == 10
    c.post(f"/api/cases/{cid}/replies/send", json={})
    assert next(i for i in c.get("/api/inbox").json() if i["case_id"] == cid)["reply"] == "sent"

    cid2, _ = run_scenario(c, "double_billing")
    row2 = next(i for i in c.get("/api/inbox").json() if i["case_id"] == cid2)
    assert row2["status"] == "AWAITING_APPROVAL" and row2["approvals"] >= 1
