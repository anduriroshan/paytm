"""Forensics over the seeded demo data: trail, statements, suspects, verdict, sweeps and the read-only probe guard."""
from __future__ import annotations

import re

import pytest

from munim import config
from munim.db.conn import connect
from munim.db.scenarios import SCENARIOS, get_scenario
from munim.forensics import probe as probe_mod
from munim.forensics.probe import run_probe
from munim.forensics.schema_card import schema_card
from munim.forensics.statements import check_statements
from munim.forensics.suspects import CATALOGUE, find_victims, interrogate
from munim.forensics.trail import MAX_NODES, build_trail
from munim.forensics.verdict import decide_verdict
from munim.llm import mock
from munim.models import ProbePlan

TODAY = config.DEMO_TODAY

# scenario -> (outcome, culprit, damages_paise, number of damaged ledger rows)
EXPECTED = {
    "zombie_rent": ("REFUND_DUE", "ZOMBIE_BILLING", 99800, 2),
    "double_billing": ("REFUND_DUE", "DUPLICATE_SUBSCRIPTION", 249500, 5),
    "duplicate_edi": ("REFUND_DUE", "DUPLICATE_EDI", 125000, 1),
    "legit_rent": ("NO_FAULT", "PICKUP_NOT_COMPLETED", 0, 0),
    "risk_hold": ("HOLD_EXPLAINED", "RISK_HOLD", 0, 0),
    "kyc_hold": ("HOLD_EXPLAINED", "KYC_HOLD", 0, 0),
    "prompt_injection": ("NO_EVIDENCE", None, 0, 0),
    "cold_case": ("IMPROVISED", None, 0, 0),
}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    from munim.db.seed import reset_db
    path = tmp_path_factory.mktemp("forensics") / "munim_forensics.db"
    reset_db(path)
    return path


@pytest.fixture()
def conn(db):
    c = connect(db)
    yield c
    c.close()


def intake_for(sc):
    return mock.intake({"scenario_id": sc.id, "complaint_text": sc.complaint_text, "today": TODAY})


def investigate(conn, scenario_id, intake=None):
    sc = get_scenario(scenario_id)
    intake = intake or intake_for(sc)
    trail = build_trail(conn, sc.merchant_id, intake, TODAY)
    checks = check_statements(conn, sc.merchant_id, intake, trail, TODAY)
    suspects = interrogate(conn, sc.merchant_id, intake, trail, TODAY)
    return sc, intake, trail, checks, suspects


def improvise(sc, intake, db_path):
    """Same loop as the graph's improvise node, with the offline mock planner and run_probe on the test DB."""
    probes, plan = [], None
    for step in range(1, config.MAX_PROBES + 2):
        ctx = {"complaint_text": sc.complaint_text, "intake": intake.model_dump(mode="json"), "merchant_id": sc.merchant_id,
               "today": TODAY, "schema_card": schema_card(), "probes": [p.model_dump(mode="json") for p in probes],
               "step": step, "max_steps": config.MAX_PROBES, "scenario_id": sc.id}
        plan = mock.improvise(ctx)
        if plan.done or not plan.sql:
            break
        if len(probes) >= config.MAX_PROBES:
            plan = ProbePlan(done=True, finding=plan.finding, evidence_ledger_ids=plan.evidence_ledger_ids)
            break
        probes.append(run_probe(plan.sql, sc.merchant_id, step, plan.purpose, db_path=db_path))
    if plan is None or not plan.done:
        plan = ProbePlan(done=True)
    seen = {str(v) for p in probes for row in p.rows for v in row}
    plan = plan.model_copy(update={"evidence_ledger_ids": [i for i in plan.evidence_ledger_ids if str(i) in seen]})
    return plan, probes


def row_exists(conn, table, row_id) -> bool:
    key = int(row_id) if str(row_id).isdigit() else row_id
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (key,)).fetchone() is not None


def one_sentence(text: str) -> bool:
    return bool(text) and "\n" not in text and "  " not in text and text.rstrip().endswith((".", ")"))


# ───────────────────────────── all scenarios ─────────────────────────────

@pytest.mark.parametrize("scenario_id", [s.id for s in SCENARIOS])
def test_scenario_pipeline(conn, db, scenario_id):
    sc, intake, trail, checks, suspects = investigate(conn, scenario_id)
    outcome, culprit, damages, n_ids = EXPECTED[scenario_id]

    # trail: bounded, consistent, rooted in real rows
    ids = {n.id for n in trail.nodes}
    assert 0 < len(trail.nodes) <= MAX_NODES
    assert len(ids) == len(trail.nodes)
    assert all(e.source in ids and e.target in ids and e.label for e in trail.edges)
    assert set(trail.disputed) <= ids
    assert trail.window_start <= trail.window_end
    for n in trail.nodes:
        if n.kind not in ("payments_summary",) and ".." not in n.row_id and n.kind != "action":
            assert row_exists(conn, n.table, n.row_id), n.id
    for nid in trail.disputed:
        assert "DISPUTED" in next(n for n in trail.nodes if n.id == nid).flags

    # statements: one per claim, plain sentences
    assert len(checks) == len(intake.claims)
    assert all(one_sentence(c.finding) for c in checks), [c.finding for c in checks]

    # suspects: all 7, MATCHes first, one-sentence summaries, real evidence
    assert sorted(s.suspect_id for s in suspects) == sorted(CATALOGUE)
    statuses = [s.status for s in suspects]
    assert statuses == sorted(statuses, key=lambda st: st != "MATCH")
    for s in suspects:
        assert one_sentence(s.summary), (s.suspect_id, s.summary)
        for ref in s.evidence:
            assert row_exists(conn, ref.table, ref.row_id), (s.suspect_id, ref)

    plan, probes = (None, [])
    if not any(s.status == "MATCH" for s in suspects):
        plan, probes = improvise(sc, intake, db)
    v = decide_verdict(suspects, intake, plan, probes)

    assert v.outcome == outcome == sc.expect["outcome"]
    if culprit:
        assert culprit in v.culprits
    assert one_sentence(v.headline), v.headline
    assert v.damages_paise == damages
    assert len(v.damages_ledger_ids) == n_ids
    if v.damages_ledger_ids:   # damages come only from the ledger: unrefunded DEBITs of this merchant
        marks = ",".join("?" * len(v.damages_ledger_ids))
        rows = conn.execute(f"SELECT merchant_id, direction, amount_paise, id FROM ledger_entries WHERE id IN ({marks})",
                            v.damages_ledger_ids).fetchall()
        assert len(rows) == n_ids
        assert {r["merchant_id"] for r in rows} == {sc.merchant_id}
        assert {r["direction"] for r in rows} == {"DEBIT"}
        assert sum(r["amount_paise"] for r in rows) == damages
        assert set(v.damages_ledger_ids) <= {int(n.row_id) for n in trail.nodes if n.id in trail.disputed}
    for ref in v.smoking_gun:
        assert row_exists(conn, ref.table, ref.row_id), ref


def test_zombie_statements_and_damages(conn):
    _, _, trail, checks, suspects = investigate(conn, "zombie_rent")
    by_kind = {c.claim.kind: c for c in checks}
    assert by_kind["DEVICE_RETURNED"].status == "VERIFIED"
    assert by_kind["DEDUCTION_COUNT"].status == "PARTLY_TRUE"
    assert "12 Sep 2026" in by_kind["DEVICE_RETURNED"].finding
    zombie = next(s for s in suspects if s.suspect_id == "ZOMBIE_BILLING")
    assert zombie.status == "MATCH" and zombie.damages_paise == 99800 and len(zombie.damages_ledger_ids) == 2
    assert zombie.targets.get("subscription_id") and zombie.targets.get("device_id")
    flagged = [n for n in trail.nodes if "AFTER_PICKUP" in n.flags]
    assert sorted(int(n.row_id) for n in flagged) == sorted(zombie.damages_ledger_ids)
    assert "₹998" in decide_verdict(suspects, intake_for(get_scenario("zombie_rent"))).headline


def test_legit_rent_return_contradicted(conn):
    _, _, _, checks, suspects = investigate(conn, "legit_rent")
    returned = next(c for c in checks if c.claim.kind == "DEVICE_RETURNED")
    assert returned.status == "CONTRADICTED"
    assert "never collected" in returned.finding
    assert next(s for s in suspects if s.suspect_id == "ZOMBIE_BILLING").status == "ELIMINATED"
    assert next(s for s in suspects if s.suspect_id == "PICKUP_NOT_COMPLETED").status == "MATCH"


def test_duplicate_edi_trail_flags(conn):
    _, _, trail, _, _ = investigate(conn, "duplicate_edi")
    flags = {f for n in trail.nodes for f in n.flags}
    assert {"DISPUTED", "DUPLICATE", "TIMEOUT_BUT_DEBITED"} <= flags


def test_imprecise_intake_still_finds_zombie(conn):
    sc = get_scenario("zombie_rent")
    vague = intake_for(sc).model_copy(update={"claims": []})
    _, _, trail, checks, suspects = investigate(conn, "zombie_rent", vague)
    assert checks == []
    assert trail.disputed
    zombie = next(s for s in suspects if s.suspect_id == "ZOMBIE_BILLING")
    assert zombie.status == "MATCH" and zombie.damages_paise == 99800


# ───────────────────────────── sweeps ─────────────────────────────

def test_find_victims_zombie(conn):
    victims = find_victims(conn, "ZOMBIE_BILLING", "M1001", TODAY)
    assert 30 <= len(victims) <= 40
    mids = [v.merchant_id for v in victims]
    assert "M1001" not in mids and len(set(mids)) == len(mids)
    assert all(v.status == "MATCH" and v.damages_paise > 0 and v.damages_ledger_ids for v in victims)
    assert len(find_victims(conn, "ZOMBIE_BILLING", None, TODAY)) == len(victims) + 1


# ───────────────────────────── already refunded ─────────────────────────────

def test_refunded_zombie_becomes_no_fault(tmp_path):
    from munim.db.seed import reset_db
    path = tmp_path / "refunded.db"
    reset_db(path)
    c = connect(path)
    try:
        _, intake, _, _, suspects = investigate(c, "zombie_rent")
        ids = next(s for s in suspects if s.suspect_id == "ZOMBIE_BILLING").damages_ledger_ids
        assert len(ids) == 2
        with c:
            for i, lid in enumerate(ids, 1):
                r = c.execute("SELECT * FROM ledger_entries WHERE id = ?", (lid,)).fetchone()
                c.execute("INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, "
                          "source_type, source_id, mandate_id, value_date, posted_at, narration, refund_of) "
                          "VALUES (?, ?, 'REFUND_CREDIT', 'CREDIT', ?, 'SUCCESS', 'action', ?, NULL, ?, ?, ?, ?)",
                          (r["merchant_id"], f"RFDTEST{i:04d}", r["amount_paise"], f"test-{i}", "2026-11-14",
                           "2026-11-14T10:00:00", f"Refund of {r['txn_ref']}", lid))
        _, intake, trail, _, suspects = investigate(c, "zombie_rent")
        by_id = {s.suspect_id: s for s in suspects}
        assert by_id["ZOMBIE_BILLING"].status == "ELIMINATED"
        assert by_id["ALREADY_REFUNDED"].status == "MATCH"
        assert by_id["ALREADY_REFUNDED"].damages_paise == 0
        assert any("ALREADY_REFUNDED" in n.flags for n in trail.nodes)
        v = decide_verdict(suspects, intake)
        assert v.outcome == "NO_FAULT" and v.damages_paise == 0 and not v.damages_ledger_ids
        assert "ALREADY_REFUNDED" in v.culprits
    finally:
        c.close()


# ───────────────────────────── probe guard ─────────────────────────────

def test_probe_allows_scoped_select(db):
    p = run_probe("SELECT id, gross_paise, net_paise FROM settlements WHERE merchant_id = 'M1008' ORDER BY batch_date DESC;",
                  "M1008", 1, "recent settlements", db_path=db)
    assert p.ok, p.error
    assert p.columns == ["id", "gross_paise", "net_paise"]
    assert p.rows and all(str(r[0]).startswith("STL-1008-") for r in p.rows)


def test_probe_sees_only_this_merchant(db):
    p = run_probe("SELECT COUNT(DISTINCT merchant_id), MIN(merchant_id) FROM ledger_entries WHERE merchant_id = 'M1008' OR 1 = 1",
                  "M1008", 1, "scope", db_path=db)
    assert p.ok and p.rows == [[1, "M1008"]]


def test_probe_row_cap(db):
    p = run_probe("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 500) "
                  "SELECT n FROM r, merchants m WHERE m.id = 'M1008'", "M1008", 1, "cap", db_path=db)
    assert p.ok and len(p.rows) == config.PROBE_ROW_LIMIT


@pytest.mark.parametrize("sql", [
    "UPDATE settlements SET net_paise = 0 WHERE merchant_id = 'M1008'",
    "DELETE FROM ledger_entries WHERE merchant_id = 'M1008'",
    "INSERT INTO ledger_entries(merchant_id) VALUES ('M1008')",
    "ATTACH DATABASE 'other.db' AS o",
    "SELECT 1 FROM merchants WHERE id = 'M1008'; ATTACH DATABASE 'other.db' AS o",
    "PRAGMA table_info(ledger_entries)",
    "SELECT * FROM pragma_table_info('ledger_entries') WHERE 'M1008' = 'M1008'",
    "SELECT 1 FROM merchants WHERE id = 'M1008'; SELECT 2 FROM merchants WHERE id = 'M1008'",
    "SELECT 1 FROM merchants WHERE id = 'M1008'; DELETE FROM ledger_entries",
    "SELECT * FROM actions WHERE merchant_id = 'M1008'",
    "SELECT * FROM cases WHERE merchant_id = 'M1008'",
    "SELECT * FROM \"audit_log\" WHERE 'M1008' = 'M1008'",
    "SELECT name FROM sqlite_master WHERE 'M1008' = 'M1008'",
    "SELECT COUNT(*) FROM ledger_entries",
    "SELECT * FROM ledger_entries WHERE merchant_id = M1008",
    "SELECT * FROM ledger_entries WHERE merchant_id = 'M1001'",
    "SELECT * FROM ledger_entries WHERE merchant_id IN ('M1008', 'M1001')",
    "WITH x AS (SELECT * FROM main.ledger_entries) SELECT COUNT(*) FROM x WHERE 'M1008' = 'M1008'",
    "SELECT hex(randomblob(16)) FROM merchants WHERE id = 'M1008'",
    "",
    None,
])
def test_probe_rejects(conn, db, sql):
    before = conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
    p = run_probe(sql, "M1008", 1, "attack", db_path=db)
    assert not p.ok and p.error and not p.rows
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == before


def test_probe_time_cap(db, monkeypatch):
    monkeypatch.setattr(probe_mod, "TIME_LIMIT_S", 0.3)
    p = run_probe("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
                  "SELECT COUNT(*) FROM r, merchants m WHERE m.id = 'M1008'", "M1008", 1, "runaway", db_path=db)
    assert not p.ok and "time cap" in p.error
    assert p.ms < 3000


def test_probe_never_raises(tmp_path):
    p = run_probe("SELECT 1 FROM merchants WHERE id = 'M1008'", "M1008", "x", None, db_path=tmp_path / "missing.db")
    assert not p.ok and p.error
    p = run_probe("SELECT 1 FROM merchants WHERE id = 'M1008'", "M1008; DROP", 1, "bad id")
    assert not p.ok and p.error


# ───────────────────────────── S8 cold case ─────────────────────────────

def test_cold_case_probes_find_chargeback(db):
    day = "2026-11-13"
    stl = run_probe(mock._settlement_probe_sql("M1008", day), "M1008", 1, "settlement breakdown", db_path=db)
    assert stl.ok, stl.error
    row = dict(zip(stl.columns, stl.rows[0]))
    assert row["id"] == "STL-1008-20261113"
    assert (row["gross_paise"], row["net_paise"], row["deductions_paise"], row["mdr_paise"]) == (1822000, 1588000, 234000, 0)

    led = run_probe(mock._ledger_chargeback_sql("M1008", day), "M1008", 2, "ledger + chargebacks", db_path=db)
    assert led.ok, led.error
    rows = [dict(zip(led.columns, r)) for r in led.rows]
    cb = [r for r in rows if r["entry_type"] == "CHARGEBACK_DEBIT"]
    assert len(cb) == 1 and cb[0]["amount_paise"] == 234000 and cb[0]["chargeback_id"]

    sc = get_scenario("cold_case")
    intake = intake_for(sc)
    plan, probes = improvise(sc, intake, db)
    assert plan.done and plan.finding and plan.evidence_ledger_ids == [cb[0]["ledger_id"]]
    c = connect(db)
    try:
        suspects = interrogate(c, sc.merchant_id, intake, build_trail(c, sc.merchant_id, intake, TODAY), TODAY)
    finally:
        c.close()
    v = decide_verdict(suspects, intake, plan, probes)
    assert v.outcome == "IMPROVISED" and v.confidence == "LOW" and v.damages_paise == 0
    assert [r.row_id for r in v.smoking_gun if r.table == "ledger_entries"] == [str(cb[0]["ledger_id"])]
    assert "chargeback" in v.headline.lower()


def test_unsolved_when_probes_fail(conn):
    sc, intake, _, _, suspects = investigate(conn, "cold_case")
    bad = run_probe("DELETE FROM ledger_entries", sc.merchant_id, 1, "bad")
    v = decide_verdict(suspects, intake, ProbePlan(done=True), [bad])
    assert v.outcome == "UNSOLVED" and v.damages_paise == 0


def test_schema_card():
    card = schema_card()
    lines = card.splitlines()
    assert 60 <= len(lines) <= 100
    assert "paise" in card and "merchant_id = '<MERCHANT_ID>'" in card
    assert card.count("EXAMPLE") == 2
    assert not re.search(r"\b(cases|actions|audit_log)\b\s*\(", card)   # workflow tables are not described
    assert str(config.PROBE_ROW_LIMIT) in card
