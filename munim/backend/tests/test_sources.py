"""Connected sources (backend/sources/*.yaml): loading, routing, file + API readers, turning a source off.
No network: the API reader runs on httpx.MockTransport. All data is fictional."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from munim import config
from munim.forensics.probe import BUSINESS_TABLES, run_probe
from munim.models import ActionType, Claim, ClaimKind, IntakeResult, Intent
from munim.sources import connectors, registry
from munim.sources.registry import load, reach, route

SHIPPED = config.BACKEND_DIR / "sources"


def intake(intent: Intent, claims=(), text_en: str = "", summary: str = "") -> IntakeResult:
    return IntakeResult(language="en", intent=intent, summary_en=summary, text_en=text_en,
                        claims=[Claim(kind=k, text=str(k)) for k in claims])


def reached(report_or_routed) -> dict[str, list[str]]:
    items = getattr(report_or_routed, "sources", report_or_routed)
    return {(getattr(h, "spec", None) or h).id: h.why for h in items if h.reached}


@pytest.fixture()
def src_dir(tmp_path, monkeypatch):
    """A copy of the shipped sources folder that a test may edit; Munim reads it instead of backend/sources/."""
    d = tmp_path / "sources"
    shutil.copytree(SHIPPED, d)
    monkeypatch.setattr(config, "SOURCES_DIR", d)
    monkeypatch.setattr(registry, "_cache", None)
    yield d
    registry._cache = None


def turn_off(d: Path, file: str) -> None:
    p = d / file
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("\nkind:", "\nenabled: false\nkind:", 1) if "\nenabled:" not in text
                 else text.replace("enabled: true", "enabled: false"), encoding="utf-8")


# ───────────────────────────── the shipped folder ─────────────────────────────

def test_shipped_folder_loads_and_covers_every_table():
    reg = load(SHIPPED)
    assert reg.problems == []
    assert reg.enabled_tables() == frozenset(BUSINESS_TABLES)
    ids = [s.id for s in reg.sources]
    assert ids[0] == "ledger" and {"courier-scans", "lender-api"} <= set(ids)
    assert next(s for s in reg.sources if s.id == "lender-api").enabled is False      # example only: no real lender
    owned = {a for s in reg.sources for a in s.actions}
    assert owned == set(ActionType) - {ActionType.ESCALATE, ActionType.NO_ACTION}
    assert (SHIPPED / "_template.yaml").exists() and "_template.yaml" not in reg.files.values()


def test_schema_card_is_built_from_sources():
    from munim.forensics.schema_card import schema_card
    card = schema_card()
    for t in BUSINESS_TABLES:
        assert f"\n{t}(" in card
    assert "JOINS" in card and "EXAMPLE 1" in card and "EXAMPLE 2" in card
    assert "MOST RELEVANT" not in card
    hits = asyncio.run(reach(load(SHIPPED), intake(Intent.LOAN_DEDUCTION, text_en="EDI cut twice"), "", "M1003")).sources
    assert "MOST RELEVANT TO THIS COMPLAINT: merchants, mandates, ledger_entries, loans, edi_attempts" in schema_card(hits)


# ───────────────────────────── routing: when to reach what ─────────────────────────────

def test_routing_by_topic_claim_and_word():
    reg = load(SHIPPED)
    rent = reached(route(reg, intake(Intent.RENT_DEDUCTION, [ClaimKind.DEVICE_RETURNED]), "Soundbox wapas kar diya"))
    assert set(rent) == {"ledger", "devices", "courier-scans"}
    assert rent["ledger"] == ["always"]
    assert "rent complaint" in rent["devices"] and "claim: device returned" in rent["courier-scans"]

    loan = reached(route(reg, intake(Intent.LOAN_DEDUCTION, text_en="My EMI was cut twice"), ""))
    assert set(loan) == {"ledger", "loans"}                      # lender-api matches too, but it is turned off
    assert "word: emi" in loan["loans"]

    hold = reached(route(reg, intake(Intent.SETTLEMENT_ISSUE, [ClaimKind.SETTLEMENT_NOT_RECEIVED]), ""))
    assert set(hold) == {"ledger", "payouts", "holds-kyc"}

    unclear = reached(route(reg, intake(Intent.OTHER), "hello"))
    assert set(unclear) == {s.id for s in reg.enabled if s.kind == "database"}
    assert "courier-scans" not in unclear


def test_words_match_at_the_start_of_a_word_only():
    assert registry._word_hit("emi", "my emi was cut")
    assert registry._word_hit("pickup", "two pickups failed")
    assert not registry._word_hit("emi", "premium plan")
    assert not registry._word_hit("held", "upheld")


# ───────────────────────────── file sources ─────────────────────────────

def test_file_source_reads_only_the_merchants_rows(src_dir):
    reg = load(src_dir)
    rep = asyncio.run(reach(reg, intake(Intent.RENT_DEDUCTION, [ClaimKind.DEVICE_RETURNED]), "", "M1001"))
    hit = next(h for h in rep.sources if h.id == "courier-scans")
    assert hit.status == "ok" and hit.records == 3 and hit.columns[0] == "merchant_id"
    assert {r[0] for r in hit.rows} == {"M1001"} and hit.rows[0][4] == "PICKED_UP"

    other = asyncio.run(reach(reg, intake(Intent.RENT_DEDUCTION, [ClaimKind.DEVICE_RETURNED]), "", "M1999"))
    assert next(h for h in other.sources if h.id == "courier-scans").records == 0


def test_file_source_errors_never_break_the_case(src_dir):
    (src_dir / "files" / "courier_scans.csv").unlink()
    rep = asyncio.run(reach(load(src_dir), intake(Intent.RENT_DEDUCTION, [ClaimKind.DEVICE_RETURNED]), "", "M1001"))
    hit = next(h for h in rep.sources if h.id == "courier-scans")
    assert hit.status == "error" and "file not found" in hit.error

    (src_dir / "escape.yaml").write_text("id: escape\nname: Escape\nkind: file\nwhen: {always: true}\n"
                                         "file: {path: ../../secret.csv}\n", encoding="utf-8")
    rep = asyncio.run(reach(load(src_dir), intake(Intent.OTHER), "", "M1001"))
    assert "inside the sources folder" in next(h for h in rep.sources if h.id == "escape").error


def test_json_file_with_nested_rows_and_row_cap(src_dir):
    (src_dir / "files" / "notes.json").write_text(json.dumps({"data": {"items": [
        {"merchant_id": "M1001", "n": i, "text": "x" * 500} for i in range(30)] + [{"merchant_id": "M2", "n": 99}]}}),
        encoding="utf-8")
    (src_dir / "notes.yaml").write_text("id: notes\nname: Notes\nkind: file\nmax_rows: 5\nwhen: {words: [note]}\n"
                                        "file: {path: files/notes.json, rows_at: data.items}\n", encoding="utf-8")
    rep = asyncio.run(reach(load(src_dir), intake(Intent.OTHER, text_en="see the note"), "", "M1001"))
    hit = next(h for h in rep.sources if h.id == "notes")
    assert hit.records == 5 and hit.why == ["word: note"]
    assert len(hit.rows[0][2]) == connectors.MAX_CELL                    # long text is cut


# ───────────────────────────── api sources (mocked network) ─────────────────────────────

def _api_source(d: Path, extra: str = "") -> None:
    (d / "partner.yaml").write_text(
        "id: partner\nname: Partner API\nkind: api\nwhen: {topics: [loan]}\nmax_rows: 3\n"
        "api:\n  url: https://partner.example.com/v1/m/{merchant_id}/loans\n"
        "  headers:\n    Authorization: Bearer ${PARTNER_TEST_KEY}\n  rows_at: result.loans\n" + extra, encoding="utf-8")


def test_api_source_reads_with_key_from_env(src_dir, monkeypatch):
    _api_source(src_dir)
    monkeypatch.setenv("PARTNER_TEST_KEY", "test-key-123")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        loans = [{"merchant_id": "M1003", "loan": f"L{i}", "status": "ACTIVE"} for i in range(5)]
        return httpx.Response(200, json={"result": {"loans": loans + [{"merchant_id": "M9999", "loan": "X"}]}})

    monkeypatch.setattr(connectors, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    rep = asyncio.run(reach(load(src_dir), intake(Intent.LOAN_DEDUCTION), "", "M1003"))
    hit = next(h for h in rep.sources if h.id == "partner")
    assert hit.status == "ok" and hit.records == 3 and hit.why == ["loan complaint"]
    assert seen == {"url": "https://partner.example.com/v1/m/M1003/loans", "auth": "Bearer test-key-123"}
    assert "test-key-123" not in json.dumps(rep.model_dump())


@pytest.mark.parametrize("case,expect", [
    ("no_key", "set PARTNER_TEST_KEY in backend/.env"),
    ("http_500", "HTTP 500"),
    ("timeout", "no answer within"),
    ("not_json", "not JSON"),
    ("wrong_path", "'result.loans' was not found"),
])
def test_api_source_failures_are_reported_not_raised(src_dir, monkeypatch, case, expect):
    _api_source(src_dir)
    if case != "no_key":
        monkeypatch.setenv("PARTNER_TEST_KEY", "k")
    else:
        monkeypatch.delenv("PARTNER_TEST_KEY", raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        if case == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if case == "http_500":
            return httpx.Response(500)
        if case == "not_json":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"other": []})

    monkeypatch.setattr(connectors, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    rep = asyncio.run(reach(load(src_dir), intake(Intent.LOAN_DEDUCTION), "", "M1003"))
    hit = next(h for h in rep.sources if h.id == "partner")
    assert hit.status == "error" and expect in hit.error


# ───────────────────────────── bad files are isolated ─────────────────────────────

def test_a_broken_file_is_reported_and_the_rest_still_load(src_dir):
    bad = {
        "broken.yaml": "id: [unclosed\n",
        "topic.yaml": "id: topic\nname: T\nkind: file\nwhen: {topics: [rnt]}\nfile: {path: files/x.csv}\n",
        "dup-table.yaml": "id: dup\nname: D\nkind: database\ntables: [{name: loans, about: x}]\n",
        "unknown-table.yaml": "id: unk\nname: U\nkind: database\ntables: [{name: cases, about: x}]\n",
        "secret.yaml": "id: secret\nname: S\nkind: api\napi:\n  url: https://x.example.com\n  headers:\n    X-Api-Key: abc123\n",
        "own-escalate.yaml": "id: esc\nname: E\nkind: file\nfile: {path: files/x.csv}\nactions: [ESCALATE]\n",
        "same-id.yaml": "id: ledger\nname: L\nkind: file\nfile: {path: files/x.csv}\n",
    }
    for name, text in bad.items():
        (src_dir / name).write_text(text, encoding="utf-8")
    reg = load(src_dir)
    problems = "\n".join(reg.problems)
    assert len(reg.problems) == len(bad), problems
    assert "not readable YAML" in problems and "'rnt' is not a topic" in problems
    assert "table 'loans' already belongs to loans.yaml" in problems
    assert "'cases' is not one of Munim's business tables" in problems
    assert "looks like a secret" in problems and "abc123" not in problems
    assert "ESCALATE is always available" in problems
    assert "id 'ledger' is already used" in problems
    assert reg.enabled_tables() == frozenset(BUSINESS_TABLES)


def test_folder_is_re_read_when_a_file_changes(src_dir):
    assert "loans" in registry.current().enabled_tables()
    turn_off(src_dir, "loans.yaml")
    assert "loans" not in registry.current().enabled_tables()


# ───────────────────────────── turning a source off ─────────────────────────────

def test_turned_off_database_source_is_invisible_to_the_ai(src_dir, tmp_path):
    from munim.db.seed import reset_db
    from munim.forensics.schema_card import schema_card
    turn_off(src_dir, "loans.yaml")
    card = schema_card()
    assert "\nloans(" not in card and "\nedi_attempts(" not in card and "\nledger_entries(" in card
    db = tmp_path / "probe.db"
    reset_db(db)
    tables = registry.current().enabled_tables()
    p = run_probe("SELECT id FROM loans WHERE merchant_id = 'M1003'", "M1003", 1, "t", db_path=db, tables=tables)
    assert not p.ok and "not connected" in p.error
    p = run_probe("SELECT l.id FROM main.loans l WHERE l.merchant_id = 'M1003'", "M1003", 1, "t", db_path=db, tables=tables)
    assert not p.ok                                              # the engine guard refuses it too
    p = run_probe("SELECT COUNT(*) FROM ledger_entries WHERE merchant_id = 'M1003'", "M1003", 1, "t", db_path=db, tables=tables)
    assert p.ok and p.rows[0][0] > 0


def test_device_events_stay_scoped_without_devices_table(tmp_path):
    from munim.db.seed import reset_db
    db = tmp_path / "probe.db"
    reset_db(db)
    sql = "SELECT COUNT(*) FROM device_events WHERE 'M1001' <> ''"
    alone = run_probe(sql, "M1001", 1, "t", db_path=db, tables={"device_events"})
    full = run_probe(sql, "M1001", 1, "t", db_path=db)
    assert alone.ok and full.ok and alone.rows[0][0] == full.rows[0][0] > 0


# ───────────────────────────── end to end (offline mock) ─────────────────────────────

@pytest.fixture()
def demo_db(tmp_path, monkeypatch):
    from munim.db.seed import reset_db
    from munim.graph import runner
    db = tmp_path / "munim_src.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "FORCE_MOCK", True)
    monkeypatch.setattr(config, "MOCK_STREAM_DELAY_MS", 0)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    reset_db(db)
    runner.RUNS.clear()
    yield db
    runner.RUNS.clear()


def _run(scenario_id: str):
    from munim.db.scenarios import get_scenario
    from munim.graph import runner
    sc = get_scenario(scenario_id)
    case_id, merchant = runner.create_case(sc.merchant_id, sc.complaint_text, sc.channel, sc.id)
    asyncio.run(runner.run_case(case_id, merchant, sc.complaint_text, sc.channel, sc.id))
    return runner.RUNS[case_id]


def test_case_emits_where_it_looked(demo_db, src_dir):
    from munim.graph import runner
    from munim.llm.prompts import build_messages
    run = _run("zombie_rent")
    types = [str(e.type) for e in run.events]
    assert types.index("sources") == types.index("trail") + 1
    rep = next(e.data for e in run.events if str(e.type) == "sources")
    got = {h["id"]: h for h in rep["sources"]}
    assert got["ledger"]["reached"] and got["devices"]["records"] > 0
    assert got["courier-scans"]["records"] == 3 and not got["loans"]["reached"]
    ctx = runner.case_context(run)
    ctx["question"] = "Did the courier pick it up?"
    case_file = build_messages("chat", ctx)[1]["content"]
    assert "Sources Munim looked at: Money ledger (always)" in case_file
    assert "<<<OUTSIDE" in case_file and "DLV88123401" in case_file


def test_turned_off_source_blocks_its_fixes(demo_db, src_dir):
    from munim.graph.nodes import allowed_catalogue
    turn_off(src_dir, "holds-kyc.yaml")
    offered = {a["type"] for a in allowed_catalogue()}
    assert not offered & {"SEND_KYC_LINK", "RELEASE_HOLD"} and {"ESCALATE", "NO_ACTION", "REFUND_DEBITS"} <= offered
    run = _run("kyc_hold")
    fw = next(e.data for e in run.events if str(e.type) == "firewall")
    kyc = [a for a in fw["actions"] if a["proposed"]["action_type"] == "SEND_KYC_LINK"]
    assert kyc                                       # the scripted planner ignores the list and still asks
    for a in kyc:
        assert a["decision"] == "BLOCK"
        allow = next(c for c in a["checks"] if c["id"] == "allowlist")
        assert allow["status"] == "FAIL" and "holds-kyc.yaml" in allow["detail"]


def test_sources_api_never_shows_secrets(demo_db, src_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from munim.api.main import app
    _api_source(src_dir)
    monkeypatch.setenv("PARTNER_TEST_KEY", "very-secret-value")
    monkeypatch.delenv("LENDER_API_KEY", raising=False)
    with TestClient(app) as c:
        r = c.get("/api/sources")
    assert r.status_code == 200 and "very-secret-value" not in r.text
    body = r.json()
    partner = next(s for s in body["sources"] if s["id"] == "partner")
    assert partner["detail"]["headers"] == ["Authorization"] and partner["warnings"] == []
    lender = next(s for s in body["sources"] if s["id"] == "lender-api")
    assert lender["enabled"] is False and "set LENDER_API_KEY in backend/.env" in lender["warnings"]
    assert body["problems"] == []
