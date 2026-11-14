"""End-to-end: every demo scenario through the real LangGraph pipeline, in offline mock mode (no network, no credits)."""
from __future__ import annotations

import asyncio
import time

import pytest

from munim import config
from munim.db.conn import connect, one, rows
from munim.events import NODES


@pytest.fixture()
def demo_db(tmp_path, monkeypatch):
    from munim.db.seed import reset_db
    from munim.graph import runner
    db = tmp_path / "munim_e2e.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "FORCE_MOCK", True)
    monkeypatch.setattr(config, "MOCK_STREAM_DELAY_MS", 0)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    reset_db(db)
    runner.RUNS.clear()
    yield db
    runner.RUNS.clear()


def run_scenario(scenario_id: str):
    from munim.db.scenarios import get_scenario
    from munim.graph import runner
    sc = get_scenario(scenario_id)
    assert sc, scenario_id
    case_id, merchant = runner.create_case(sc.merchant_id, sc.complaint_text, sc.channel, sc.id)
    state = asyncio.run(runner.run_case(case_id, merchant, sc.complaint_text, sc.channel, sc.id))
    return case_id, state, runner.RUNS[case_id].events


def types(events):
    return [str(e.type) for e in events]


def node_statuses(events):
    out: dict[str, str] = {}
    for e in events:
        if e.type == "node":
            out[e.data["node"]] = e.data["status"]
    return out


def ids_of_scenarios():
    from munim.db.scenarios import SCENARIOS
    return [s.id for s in SCENARIOS]


EXPECT = {
    # scenario: (outcome, firewall decision, case status)
    "zombie_rent": ("REFUND_DUE", "CORRECTED", "RESOLVED"),
    "double_billing": ("REFUND_DUE", "NEEDS_HUMAN", "AWAITING_APPROVAL"),
    "duplicate_edi": ("REFUND_DUE", "ALLOW", "RESOLVED"),
    "legit_rent": ("NO_FAULT", "ALLOW", "RESOLVED"),
    "risk_hold": ("HOLD_EXPLAINED", "BLOCK", "ESCALATED"),
    "kyc_hold": ("HOLD_EXPLAINED", "ALLOW", "RESOLVED"),
    "prompt_injection": ("NO_EVIDENCE", "BLOCK", "BLOCKED"),
    "cold_case": ("IMPROVISED", "ALLOW", "ESCALATED"),
    "stuck_return": ("REFUND_DUE", "ALLOW", "RESOLVED"),
    "bus_turned_back": ("REFUND_DUE", "ALLOW", "RESOLVED"),
    "flight_refund": ("REFUND_DUE", "NEEDS_HUMAN", "AWAITING_APPROVAL"),
}
PASSENGER_SCENARIOS = {"bus_turned_back", "flight_refund"}


def test_scenario_ids_match_contract():
    assert set(ids_of_scenarios()) == set(EXPECT)


@pytest.mark.parametrize("scenario_id", list(EXPECT))
def test_scenario_end_to_end(demo_db, scenario_id):
    outcome, decision, status = EXPECT[scenario_id]
    case_id, state, events = run_scenario(scenario_id)
    t = types(events)

    assert "error" not in t, [e.data for e in events if e.type == "error"]
    assert t[0] == "case" and t[-1] == "done"
    assert state["verdict"]["outcome"] == outcome
    assert state["firewall"]["decision"] == decision
    assert events[-1].data["status"] == status
    # every node reported (ran or skipped), in a sensible order
    ns = node_statuses(events)
    assert set(ns) == {n for n, _, _ in NODES}, ns
    assert all(s in ("end", "skip") for s in ns.values()), ns
    for needed in ("intake", "hunch", "trail", "statements", "suspect", "verdict", "proposal", "firewall", "execution", "message", "llm", "llm_done"):
        assert needed in t, needed
    assert t.index("intake") < t.index("trail") < t.index("statements") < t.index("suspect") < t.index("verdict") < t.index("proposal") < t.index("firewall") < t.index("execution")
    # full lineup emitted (shops: 8 suspects; passengers: the 4 travel ones plus ALREADY_REFUNDED)
    assert t.count("suspect") == (5 if scenario_id in PASSENGER_SCENARIOS else 8)
    # under FORCE_MOCK the streamed tokens name the scripted brain, matching llm_done (not the configured provider)
    named = {e.data["provider"] for e in events if e.type == "llm" and e.data.get("provider")}
    assert named <= {"mock"}, named
    # the merchant got a reply, streamed by the writer
    reply = [e for e in events if e.type == "message" and e.data["role"] == "munim"]
    assert reply and reply[-1].data["text"].strip()
    # seq is gapless
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    # case row updated
    with connect(demo_db) as conn:
        row = one(conn, "SELECT status, outcome, reply_text FROM cases WHERE id=?", (case_id,))
    assert row["status"] == status and row["outcome"] == outcome and row["reply_text"]


def test_s1_correction_refund_cancel_and_sweep(demo_db):
    case_id, state, events = run_scenario("zombie_rent")
    av = {a["proposed"]["action_type"]: a for a in state["firewall"]["actions"]}
    refund = av["REFUND_DEBITS"]
    assert refund["proposed"]["amount_paise"] == 149700
    assert refund["final"]["amount_paise"] == 99800
    assert refund["decision"] == "CORRECTED"
    assert av["CANCEL_SUBSCRIPTION"]["decision"] in ("ALLOW", "CORRECTED")
    with connect(demo_db) as conn:
        credits = rows(conn, "SELECT amount_paise FROM ledger_entries WHERE merchant_id='M1001' AND entry_type='REFUND_CREDIT'")
        sub = one(conn, "SELECT status FROM subscriptions WHERE id='SUB-1001-A'")
    assert sum(c["amount_paise"] for c in credits) == 99800 and len(credits) == 2
    assert sub["status"] == "CANCELLED"
    sweep = [e for e in events if e.type == "sweep"]
    assert sweep, "S1 should trigger a fleet sweep"
    assert 30 <= sweep[0].data["merchant_count"] <= 40
    assert sweep[0].data["sweep_id"]


def test_s1_rerun_is_blocked_no_double_refund(demo_db):
    run_scenario("zombie_rent")
    with connect(demo_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE entry_type='REFUND_CREDIT'").fetchone()[0]
    _, state, events = run_scenario("zombie_rent")
    assert state["verdict"]["outcome"] == "NO_FAULT"
    assert "ALREADY_REFUNDED" in state["verdict"]["culprits"]
    refund = [a for a in state["firewall"]["actions"] if a["proposed"]["action_type"] == "REFUND_DEBITS"]
    assert refund and refund[0]["decision"] == "BLOCK"
    with connect(demo_db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE entry_type='REFUND_CREDIT'").fetchone()[0]
    assert after == before
    assert "sweep" not in types(events)


def test_s2_approval_executes_once(demo_db):
    from munim.actions.executor import approve, pending
    from munim.db.conn import tx
    from munim.graph import runner
    case_id, state, _ = run_scenario("double_billing")
    with tx() as conn:
        queue = [p for p in pending(conn) if p["case_id"] == case_id]
    assert len(queue) == 1 and queue[0]["action_type"] == "REFUND_DEBITS"
    aid = queue[0]["action_id"]
    with tx() as conn:
        res = approve(conn, aid, "ops.lead")
    assert res.status == "EXECUTED" and res.amount_paise == 249500
    msg = asyncio.run(runner.followup_after_approval(aid, res, "ops.lead"))
    assert msg and msg["text"]
    with tx() as conn:
        again = approve(conn, aid, "ops.lead")
    assert again.status != "EXECUTED" or again.ledger_entry_ids == []
    with connect(demo_db) as conn:
        total = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE merchant_id='M1002' AND entry_type='REFUND_CREDIT'").fetchone()[0]
    assert total == 249500


def test_s5_risk_hold_never_released(demo_db):
    _, state, _ = run_scenario("risk_hold")
    rel = [a for a in state["firewall"]["actions"] if a["proposed"]["action_type"] == "RELEASE_HOLD"]
    assert rel and rel[0]["decision"] == "BLOCK"
    with connect(demo_db) as conn:
        assert one(conn, "SELECT status FROM holds WHERE id='HLD-1005-01'")["status"] == "ACTIVE"


def test_s7_injection_blocked(demo_db):
    _, state, events = run_scenario("prompt_injection")
    assert state["intake"]["injection_suspected"] is True
    assert state["hunch"]["injection_score"] >= 0.9
    assert all(a["decision"] == "BLOCK" for a in state["firewall"]["actions"] if a["proposed"]["action_type"] == "REFUND_DEBITS")
    with connect(demo_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE merchant_id='M1007' AND entry_type='REFUND_CREDIT'").fetchone()[0] == 0


def test_s8_improvises_with_probes(demo_db):
    _, state, events = run_scenario("cold_case")
    probes = [e for e in events if e.type == "probe"]
    assert len(probes) == 2 and all(p.data["ok"] for p in probes)
    assert state["verdict"]["confidence"] == "LOW"
    assert node_statuses(events)["improvise"] == "end"


def test_s9_stuck_return_refund_cancel_pickup_and_sweep(demo_db):
    _, state, events = run_scenario("stuck_return")
    assert state["verdict"]["culprits"] == ["RETURN_STUCK"]
    av = {a["proposed"]["action_type"]: a for a in state["firewall"]["actions"]}
    assert av["REFUND_DEBITS"]["final"]["amount_paise"] == 89700
    assert {"CANCEL_SUBSCRIPTION", "SCHEDULE_PICKUP"} <= set(av)
    assert all(a["decision"] in ("ALLOW", "CORRECTED") for a in av.values())
    with connect(demo_db) as conn:
        credits = rows(conn, "SELECT amount_paise FROM ledger_entries WHERE merchant_id='M1009' AND entry_type='REFUND_CREDIT'")
        sub = one(conn, "SELECT status FROM subscriptions WHERE merchant_id='M1009' AND device_id='D50009'")
        ticket = one(conn, "SELECT status FROM pickup_tickets WHERE device_id='D50009' AND id LIKE 'PK-%-M%'")
    assert sum(c["amount_paise"] for c in credits) == 89700 and len(credits) == 3
    assert sub["status"] == "CANCELLED" and ticket["status"] == "SCHEDULED"
    reply = [e for e in events if e.type == "message" and e.data["role"] == "munim"][-1].data["text"]
    assert "22 अगस्त" in reply and "₹897" in reply and "पिकअप" in reply
    sweep = [e for e in events if e.type == "sweep"]
    assert sweep and sweep[0].data["merchant_count"] >= 5


def test_api_flow(demo_db):
    from fastapi.testclient import TestClient
    from munim.api.main import app
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["ok"] and h["counts"]["merchants"] > 100
        assert set(h["providers"]) >= {"reasoner", "writer", "decider"}
        sc = c.get("/api/scenarios").json()
        assert len(sc) == 11 and sc[0]["merchant"]["business_name"]
        assert c.get("/api/merchants", params={"q": "Sharma"}).json()[0]["id"] == "M1001"
        s1 = next(s for s in sc if s["id"] == "zombie_rent")
        cid = c.post("/api/cases", json={"merchant_id": s1["merchant_id"], "complaint_text": s1["complaint_text"], "scenario_id": s1["id"]}).json()["case_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            evs = c.get(f"/api/cases/{cid}").json()["events"]
            if evs and evs[-1]["type"] == "done":
                break
            time.sleep(0.1)
        assert evs[-1]["type"] == "done"
        # SSE replays the full history and closes after done
        with c.stream("GET", f"/api/cases/{cid}/events") as r:
            body = "".join(r.iter_text())
        assert body.count("event: ") == len(evs) and "event: done" in body
        assert c.get("/api/cases").json()[0]["id"] == cid
        pb = c.get("/api/merchants/M1001/passbook").json()
        assert any(p["entry_type"] == "REFUND_CREDIT" for p in pb)
        m = c.get("/api/metrics").json()
        assert m["refunded_paise"] >= 99800
        # sweep approval through the API
        sweep_id = next(e for e in evs if e["type"] == "sweep")["data"]["sweep_id"]
        res = c.post(f"/api/sweeps/{sweep_id}/approve", json={}).json()
        assert res["executed"] >= 30 and res["total_paise"] > 0
        assert c.post("/api/cases", json={"merchant_id": "NOPE", "complaint_text": "x"}).status_code == 404
