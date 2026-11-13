"""Action Firewall + executor + sweep, on a tiny hand-built database (no dependency on the demo seed).

"LLM proposes, rules dispose": every test hands the firewall a proposal (often a hostile one) and checks what the
deterministic code decided, what it wrote, and, just as important, what it did NOT write.
"""
from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from munim.actions.executor import approve, metrics, pending, record_and_execute, reject
from munim.actions.sweep import approve_sweep, create_sweep
from munim.db.conn import connect, create_schema, one, rows
from munim.firewall.policy import CHECK_LABELS, CHECK_ORDER, evaluate
from munim.models import (
    ActionType,
    ActionVerdict,
    Decision,
    FirewallCheck,
    FirewallResult,
    Hunch,
    IntakeResult,
    Proposal,
    ProposedAction,
    SuspectResult,
    Verdict,
)

A = ActionType
TODAY = "2026-11-14"
_ref = itertools.count(1)


# ───────────────────────────── tiny world ─────────────────────────────

def _merchant(c, mid, name, city="Jaipur"):
    c.execute("INSERT INTO merchants(id, business_name, owner_name, phone, city, language, category, onboarded_at) VALUES (?,?,?,?,?,?,?,?)",
              (mid, name, f"Owner {mid}", "98XXXX0000", city, "hi", "kirana", "2025-01-01"))


def _device(c, did, merchant_id, status):
    c.execute("INSERT INTO devices(id, serial_no, model, merchant_id, status, installed_at) VALUES (?,?,?,?,?,?)",
              (did, f"SB4G-{did}", "Soundbox 4G", merchant_id, status, "2026-01-01"))


def _sub(c, sid, merchant_id, device_id, started="2026-03-01", status="ACTIVE"):
    c.execute("INSERT INTO subscriptions(id, merchant_id, device_id, plan_id, status, started_at) VALUES (?,?,?,?,?,?)",
              (sid, merchant_id, device_id, "PLAN_SB499", status, started))


def _debit(c, merchant_id, day, amount=49900, entry_type="RENT_DEBIT", source_id="SUB", mandate=None) -> int:
    what = "Soundbox rent" if entry_type == "RENT_DEBIT" else "Loan EDI"
    cur = c.execute(
        """INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, source_type, source_id,
                                      mandate_id, value_date, posted_at, narration) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (merchant_id, f"TXN{next(_ref):07d}", entry_type, "DEBIT", amount, "SUCCESS",
         "subscription" if entry_type == "RENT_DEBIT" else "loan", source_id, mandate, day, f"{day}T06:00:00",
         f"{what} {day[:7]} · {source_id}"),
    )
    return int(cur.lastrowid)


def _manual_refund(c, merchant_id, of_id, amount=49900, day="2026-11-13") -> int:
    """A refund someone else (an L1 agent) posted outside Munim."""
    cur = c.execute(
        """INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, source_type, source_id,
                                      value_date, posted_at, narration, refund_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (merchant_id, f"MAN{next(_ref):07d}", "REFUND_CREDIT", "CREDIT", amount, "SUCCESS", "subscription", "L1-manual",
         day, f"{day}T12:00:00", "Manual refund by L1 agent", of_id),
    )
    return int(cur.lastrowid)


def _case(c, cid, merchant_id, status="INVESTIGATING", text="Paisa kat raha hai, device to wapas kar diya tha"):
    c.execute("INSERT INTO cases(id, merchant_id, channel, language, complaint_text, status, created_at) VALUES (?,?,?,?,?,?,?)",
              (cid, merchant_id, "chat", "hi", text, status, f"{TODAY}T10:00:00"))


def _seed(c) -> SimpleNamespace:
    w = SimpleNamespace()
    c.execute("INSERT INTO plans VALUES ('PLAN_SB499', 'Soundbox 4G Rental', 'Soundbox 4G', 49900)")
    for mid, name in [("M1001", "Sharma Kirana Store"), ("M1002", "Gupta Medicals"), ("M1004", "Reddy Electronics"),
                      ("M1005", "Khan Mobile Point"), ("M1006", "Iyer Pharmacy"), ("M1007", "Verma Traders"),
                      ("M1009", "Patel Chemist"), ("M9999", "Somebody Else"), ("M2001", "Victim One"), ("M2002", "Victim Two"),
                      ("M2003", "Victim Three"), ("M2004", "Victim Zero"), ("M3001", "Cap Stores")]:
        _merchant(c, mid, name)

    # S1: device picked up 12 Sep, rent kept being debited
    _device(c, "D50001", None, "RETURNED")
    _sub(c, "SUB-1001-A", "M1001", "D50001")
    c.execute("INSERT INTO mandates VALUES ('MDT-1001-A','M1001','SUBSCRIPTION','SUB-1001-A',NULL,'UMRN1001A',49900,'ACTIVE','2026-03-01')")
    c.execute("INSERT INTO device_events(device_id, event_type, at, ref) VALUES ('D50001','PICKED_UP','2026-09-12','PK-1')")
    w.s1_sep = _debit(c, "M1001", "2026-09-01", source_id="SUB-1001-A", mandate="MDT-1001-A")   # legitimate
    w.s1_oct = _debit(c, "M1001", "2026-10-01", source_id="SUB-1001-A", mandate="MDT-1001-A")
    w.s1_nov = _debit(c, "M1001", "2026-11-01", source_id="SUB-1001-A", mandate="MDT-1001-A")

    # S2: duplicate subscription on the same device, 5 × ₹499
    _device(c, "D50002", "M1002", "ACTIVE")
    _sub(c, "SUB-1002-A", "M1002", "D50002", started="2026-01-10")
    _sub(c, "SUB-1002-B", "M1002", "D50002", started="2026-06-20")
    w.s2 = [_debit(c, "M1002", f"2026-{m:02d}-20", source_id="SUB-1002-B") for m in range(7, 12)]

    # pickup candidate, someone else's records
    _device(c, "D50004", "M1004", "RETURN_REQUESTED")
    _device(c, "D59999", "M9999", "ACTIVE")
    _sub(c, "SUB-9999-A", "M9999", "D59999")
    w.other = _debit(c, "M9999", "2026-10-01", source_id="SUB-9999-A")

    # S5 risk hold
    c.execute("INSERT INTO settlements VALUES ('STL-1005-20261111','M1005','2026-11-11',310000,10000,300000,'HELD',NULL,NULL)")
    c.execute("INSERT INTO holds VALUES ('HLD-1005-01','M1005','STL-1005-20261111','RISK','VELOCITY_SPIKE','Unusual spike','ACTIVE','2026-11-11',NULL)")

    # S6 KYC hold with an expired drug licence
    c.execute("INSERT INTO settlements VALUES ('STL-1006-20261110','M1006','2026-11-10',125000,5000,120000,'HELD',NULL,NULL)")
    c.execute("INSERT INTO holds VALUES ('HLD-1006-01','M1006','STL-1006-20261110','KYC','KYC_DOC_EXPIRED','Drug licence expired','ACTIVE','2026-11-10',NULL)")
    c.execute("INSERT INTO kyc_documents(merchant_id, doc_type, status, verified_at, expires_at) VALUES ('M1006','DRUG_LICENSE','EXPIRED','2021-11-01','2026-10-31')")
    c.execute("INSERT INTO kyc_documents(merchant_id, doc_type, status, verified_at, expires_at) VALUES ('M1006','PAN','VALID','2021-11-01',NULL)")

    # KYC hold whose documents are valid again: ₹800 + ₹700 held, an older batch already settled
    c.execute("INSERT INTO settlements VALUES ('STL-1009-20261111','M1009','2026-11-11',50000,0,50000,'SETTLED','UTR1','2026-11-12')")
    c.execute("INSERT INTO settlements VALUES ('STL-1009-20261112','M1009','2026-11-12',82000,2000,80000,'HELD',NULL,NULL)")
    c.execute("INSERT INTO settlements VALUES ('STL-1009-20261113','M1009','2026-11-13',71000,1000,70000,'HELD',NULL,NULL)")
    c.execute("INSERT INTO holds VALUES ('HLD-1009-01','M1009','STL-1009-20261112','KYC','KYC_DOC_EXPIRED','Licence expired','ACTIVE','2026-11-12',NULL)")
    c.execute("INSERT INTO kyc_documents(merchant_id, doc_type, status, verified_at, expires_at) VALUES ('M1009','DRUG_LICENSE','VALID','2026-11-13','2031-11-12')")

    # sweep victims of the same zombie-billing bug: 1, 2 and 3 debits; M2004 has nothing to refund
    w.victims = {}
    for n, mid in enumerate(["M2001", "M2002", "M2003", "M2004"], 1):
        _device(c, f"D5200{n}", None, "RETURNED")
        _sub(c, f"SUB-200{n}-A", mid, f"D5200{n}")
        days = ["2026-11-01", "2026-10-01", "2026-09-01"][:n] if n < 4 else []
        w.victims[mid] = [_debit(c, mid, d, source_id=f"SUB-200{n}-A") for d in days]

    # 30-day cap: ₹4,500 auto-refunded by the firewall on 01 Nov, two fresh rent debits now
    loan_debit = _debit(c, "M3001", "2026-10-20", amount=450000, entry_type="EDI_DEBIT", source_id="LN-3001")
    cur = c.execute("""INSERT INTO actions(merchant_id, action_type, amount_paise, params_json, decision, status, decided_by, created_at, executed_at)
                       VALUES ('M3001','REFUND_DEBITS',450000,'{}','ALLOW','EXECUTED','firewall','2026-11-01T10:00:00','2026-11-01T10:00:00')""")
    w.cap_action = int(cur.lastrowid)
    c.execute("""INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, source_type, source_id,
                                            value_date, posted_at, narration, refund_of)
                 VALUES ('M3001','RFD-OLD-1','REFUND_CREDIT','CREDIT',450000,'SUCCESS','action',?,'2026-11-01','2026-11-01T10:00:00','Refund of EDI',?)""",
              (str(w.cap_action), loan_debit))
    w.cap = [_debit(c, "M3001", d, source_id="SUB-3001-A") for d in ("2026-10-01", "2026-11-01")]
    return w


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "firewall.db")
    create_schema(conn)
    w = _seed(conn)
    conn.commit()
    w.conn = conn
    yield w
    conn.close()


# ───────────────────────────── helpers ─────────────────────────────

def s1_verdict(w, **kw) -> Verdict:
    base = dict(outcome="REFUND_DUE", culprits=["ZOMBIE_BILLING"], headline="Rent kept being debited after the device was picked up.",
                damages_ledger_ids=[w.s1_oct, w.s1_nov], damages_paise=99800,
                targets={"subscription_id": "SUB-1001-A", "device_id": "D50001"},
                remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION], confidence="HIGH", sweepable_suspect="ZOMBIE_BILLING")
    return Verdict(**{**base, **kw})


def s2_verdict(w) -> Verdict:
    return Verdict(outcome="REFUND_DUE", culprits=["DUPLICATE_SUBSCRIPTION"], headline="A second subscription billed the same device.",
                   damages_ledger_ids=w.s2, damages_paise=249500, targets={"subscription_id": "SUB-1002-B", "device_id": "D50002"},
                   remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION], confidence="HIGH")


def refund(ids, amount, **kw) -> ProposedAction:
    return ProposedAction(action_type=A.REFUND_DEBITS, amount_paise=amount, ledger_entry_ids=list(ids), **kw)


def judge(w, merchant_id, verdict, actions, **kw) -> FirewallResult:
    return evaluate(w.conn, merchant_id=merchant_id, proposal=Proposal(actions=actions, rationale="test"), verdict=verdict, **kw)


def run(w, merchant_id, verdict, actions, *, case_id=None, **kw):
    fr = judge(w, merchant_id, verdict, actions, case_id=case_id, **kw)
    return fr, record_and_execute(w.conn, case_id=case_id, merchant_id=merchant_id, firewall=fr, verdict=verdict)


def chk(av: ActionVerdict, check_id: str) -> FirewallCheck:
    return next(c for c in av.checks if c.id == check_id)


def scalar(w, sql, params=()):
    return w.conn.execute(sql, params).fetchone()[0]


def refund_credits(w, merchant_id):
    return rows(w.conn, "SELECT * FROM ledger_entries WHERE merchant_id = ? AND entry_type = 'REFUND_CREDIT' ORDER BY id", (merchant_id,))


def sweep_victim(w, mid, **kw) -> SuspectResult:
    ids = w.victims[mid]
    n = mid[-1]
    base = dict(suspect_id="ZOMBIE_BILLING", title="Billing continued after device return", kind="FAULT", status="MATCH",
                summary="Picked up but still billed", damages_ledger_ids=ids, damages_paise=49900 * len(ids),
                targets={"subscription_id": f"SUB-200{n}-A", "device_id": f"D5200{n}"},
                remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION], sweepable=True, merchant_id=mid)
    return SuspectResult(**{**base, **kw})


# ───────────────────────────── S1: correction + execution ─────────────────────────────

def test_s1_correction_executes_two_refunds_and_cancels(db):
    w = db
    _case(w.conn, "CASE-0001", "M1001")
    v = s1_verdict(w)
    actions = [refund([w.s1_oct, w.s1_nov], 149700, reason="3 months of wrong rent"),
               ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A")]

    before = w.conn.total_changes
    fr = judge(w, "M1001", v, actions, case_id="CASE-0001")
    assert w.conn.total_changes == before, "evaluate() must not write"

    assert fr.decision == Decision.CORRECTED
    rf, cancel = fr.actions
    assert rf.decision == Decision.CORRECTED and rf.final.amount_paise == 99800
    assert rf.final.ledger_entry_ids == [w.s1_oct, w.s1_nov]
    assert chk(rf, "amount_recompute").status == "FIXED"
    assert chk(rf, "amount_recompute").detail == (
        "LLM proposed ₹1,497 (3 × ₹499); ledger supports ₹998: rent debits on 01 Oct 2026 and 01 Nov 2026.")
    assert cancel.decision == Decision.ALLOW and cancel.final.target_id == "SUB-1001-A"
    for av in fr.actions:   # one check per id, in the documented order, short labels, one-sentence details
        assert [c.id for c in av.checks] == list(CHECK_ORDER)
        assert all(c.label == CHECK_LABELS[c.id] and c.detail.endswith(".") for c in av.checks)
        assert av.idempotency_key and len(av.idempotency_key) == 16

    res = record_and_execute(w.conn, case_id="CASE-0001", merchant_id="M1001", firewall=fr, verdict=v)
    assert [r.status for r in res] == ["EXECUTED", "EXECUTED"]
    assert res[0].amount_paise == 99800 and len(res[0].ledger_entry_ids) == 2
    assert "₹998" in res[0].detail

    credits = refund_credits(w, "M1001")
    assert len(credits) == 2 and sum(c["amount_paise"] for c in credits) == 99800
    assert {c["refund_of"] for c in credits} == {w.s1_oct, w.s1_nov}
    assert all(c["txn_ref"].startswith("RFD20261114") and c["source_type"] == "action" and c["value_date"] == TODAY for c in credits)
    assert len({c["txn_ref"] for c in credits}) == 2
    assert all(c["narration"].startswith("Refund of Soundbox rent") and "Munim CASE-0001" in c["narration"] for c in credits)
    assert {c["source_id"] for c in credits} == {str(res[0].action_id)}

    sub = one(w.conn, "SELECT * FROM subscriptions WHERE id = 'SUB-1001-A'")
    assert sub["status"] == "CANCELLED" and sub["ended_at"] == TODAY and sub["end_reason"]
    assert one(w.conn, "SELECT status FROM mandates WHERE id = 'MDT-1001-A'")["status"] == "REVOKED"

    acts = rows(w.conn, "SELECT * FROM actions WHERE case_id = 'CASE-0001' ORDER BY id")
    assert [a["status"] for a in acts] == ["EXECUTED", "EXECUTED"]
    assert json.loads(acts[0]["proposed_json"])["amount_paise"] == 149700
    assert json.loads(acts[0]["params_json"])["amount_paise"] == 99800
    assert json.loads(acts[0]["firewall_json"])["decision"] == "CORRECTED"
    assert json.loads(acts[0]["verdict_json"])["damages_paise"] == 99800
    assert all(a["idempotency_key"] and a["decided_by"] == "firewall" and a["executed_at"] for a in acts)
    actors = {r["actor"] for r in rows(w.conn, "SELECT actor FROM audit_log WHERE case_id = 'CASE-0001'")}
    assert {"firewall", "executor"} <= actors
    assert w.conn.in_transaction, "the executor must leave committing to the caller"


def test_s1_rerun_is_blocked_with_no_new_ledger_rows(db):
    w = db
    _case(w.conn, "CASE-0001", "M1001")
    run(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 149700),
                                    ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A")], case_id="CASE-0001")
    ledger_before = scalar(w, "SELECT COUNT(*) FROM ledger_entries")

    # the detective now says NO_FAULT (already refunded); a sloppy LLM still proposes the refund
    _case(w.conn, "CASE-0002", "M1001")
    nf = Verdict(outcome="NO_FAULT", culprits=["ALREADY_REFUNDED"], headline="Already refunded.", confidence="HIGH")
    fr, res = run(w, "M1001", nf, [refund([w.s1_oct, w.s1_nov], 99800)], case_id="CASE-0002")
    av = fr.actions[0]
    assert fr.decision == Decision.BLOCK and av.final is None and av.idempotency_key is None
    assert chk(av, "hard_deny").status == "FAIL" and chk(av, "remedy_match").status == "FAIL"
    assert chk(av, "idempotency").status == "FAIL" and "already refunded" in chk(av, "idempotency").detail
    assert res[0].status == "BLOCKED" and res[0].amount_paise == 0
    row = one(w.conn, "SELECT * FROM actions WHERE id = ?", (res[0].action_id,))
    assert row["status"] == "BLOCKED" and row["idempotency_key"] is None and row["decision"] == "BLOCK"

    # even replaying the stale REFUND_DUE verdict cannot pay twice: idempotency alone blocks it
    fr2, res2 = run(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 99800),
                                                ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A")], case_id="CASE-0002")
    assert [a.decision for a in fr2.actions] == [Decision.BLOCK, Decision.BLOCK]
    assert chk(fr2.actions[0], "idempotency").status == "FAIL" and chk(fr2.actions[0], "hard_deny").status == "PASS"
    assert chk(fr2.actions[1], "preconditions").status == "FAIL"     # subscription already CANCELLED
    assert scalar(w, "SELECT COUNT(*) FROM ledger_entries") == ledger_before


# ───────────────────────────── ledger ids + hostile fields ─────────────────────────────

def test_omitted_ledger_ids_are_filled_from_damages(db):
    w = db
    fr = judge(w, "M1001", s1_verdict(w), [refund([], 99800)])
    av = fr.actions[0]
    assert av.decision == Decision.CORRECTED
    assert chk(av, "evidence_binding").status == "FIXED"
    assert av.final.ledger_entry_ids == [w.s1_oct, w.s1_nov] and av.final.amount_paise == 99800


def test_extraneous_ledger_ids_are_dropped(db):
    w = db
    fr = judge(w, "M1001", s1_verdict(w), [refund([w.s1_sep, w.s1_oct, w.s1_nov], 149700)])
    av = fr.actions[0]
    assert av.decision == Decision.CORRECTED
    eb = chk(av, "evidence_binding")
    assert eb.status == "FIXED" and str(w.s1_sep) in eb.detail
    assert av.final.ledger_entry_ids == [w.s1_oct, w.s1_nov] and av.final.amount_paise == 99800


def test_other_merchants_ledger_id_fails_ownership(db):
    w = db
    for bad in (w.other, 987654):       # someone else's debit; a debit that does not exist
        fr = judge(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov, bad], 149700)])
        av = fr.actions[0]
        assert fr.decision == Decision.BLOCK and av.final is None
        assert chk(av, "ownership").status == "FAIL" and str(bad) in chk(av, "ownership").detail


def test_hostile_fields(db):
    w = db
    v = s1_verdict(w)
    # negative and absurd amounts are recomputed from the ledger
    for amount in (-500, 10**15):
        av = judge(w, "M1001", v, [refund([w.s1_oct, w.s1_nov], amount)]).actions[0]
        assert av.decision == Decision.CORRECTED and av.final.amount_paise == 99800
    # someone else's subscription, an unknown one
    for target in ("SUB-9999-A", "SUB-NOPE"):
        av = judge(w, "M1001", v, [ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id=target)]).actions[0]
        assert av.decision == Decision.BLOCK and chk(av, "ownership").status == "FAIL"
    # a type the allowlist has never heard of (bypassing pydantic, as a raw LLM dict might)
    junk = ProposedAction.model_construct(action_type="TRANSFER_FUNDS", amount_paise=10**9, ledger_entry_ids=[], target_id="acct",
                                          team=None, reason="admin")
    fr = evaluate(w.conn, merchant_id="M1001", proposal=Proposal.model_construct(actions=[junk], rationale=""), verdict=v)
    av = fr.actions[0]
    assert av.decision == Decision.BLOCK and chk(av, "allowlist").status == "FAIL"
    assert [c.status for c in av.checks[1:]] == ["SKIP"] * (len(CHECK_ORDER) - 1)
    res = record_and_execute(w.conn, case_id=None, merchant_id="M1001", firewall=fr, verdict=v)
    assert res[0].status == "BLOCKED" and one(w.conn, "SELECT action_type FROM actions WHERE id = ?", (res[0].action_id,))["action_type"] == "TRANSFER_FUNDS"
    # a non-money action carrying an amount is zeroed
    av = judge(w, "M1001", v, [ProposedAction(action_type=A.ESCALATE, team="billing", amount_paise=5000)]).actions[0]
    assert av.decision == Decision.CORRECTED and av.final.amount_paise == 0
    # empty proposal
    fr = judge(w, "M1001", v, [])
    assert fr.decision == Decision.ALLOW and fr.actions == []
    # a flood of actions: everything past the cap is refused unexamined
    fr = judge(w, "M1001", v, [ProposedAction(action_type=A.ESCALATE, team=t) for t in ["risk", "billing", "logistics", "payments", "lending"] * 2])
    assert all(a.decision == Decision.BLOCK and chk(a, "allowlist").status == "FAIL" for a in fr.actions[8:])
    assert all(a.decision == Decision.ALLOW for a in fr.actions[:5])


def test_duplicate_within_one_proposal_is_blocked(db):
    w = db
    fr = judge(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 99800), refund([w.s1_nov], 49900),
                                           ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A"),
                                           ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A")])
    assert [a.decision for a in fr.actions] == [Decision.ALLOW, Decision.BLOCK, Decision.ALLOW, Decision.BLOCK]
    assert "action 1 of this proposal" in chk(fr.actions[1], "idempotency").detail
    assert chk(fr.actions[3], "idempotency").status == "FAIL"
    assert fr.decision == Decision.BLOCK


# ───────────────────────────── limits + approvals ─────────────────────────────

def _s2_pending(w, case_status="AWAITING_APPROVAL"):
    _case(w.conn, "CASE-0002", "M1002", status=case_status)
    fr, res = run(w, "M1002", s2_verdict(w), [refund(w.s2, 249500), ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1002-B")],
                  case_id="CASE-0002")
    return fr, res


def test_over_limit_needs_human_then_approve_once(db):
    w = db
    fr, res = _s2_pending(w)
    rf = fr.actions[0]
    assert fr.decision == Decision.NEEDS_HUMAN and rf.decision == Decision.NEEDS_HUMAN
    assert chk(rf, "limits").status == "WARN" and "₹2,495" in chk(rf, "limits").detail and "₹2,000" in chk(rf, "limits").detail
    assert [r.status for r in res] == ["PENDING_APPROVAL", "EXECUTED"]
    assert refund_credits(w, "M1002") == []                       # nothing moved yet

    queue = [p for p in pending(w.conn) if p["case_id"] == "CASE-0002"]
    assert len(queue) == 1
    q = queue[0]
    assert q["action_type"] == "REFUND_DEBITS" and q["amount_paise"] == 249500 and q["business_name"] == "Gupta Medicals"
    assert q["complaint_text"] and any(c["id"] == "limits" and c["status"] == "WARN" for c in q["checks"])
    # a fresh proposal for the same debits while one is queued is refused (no second queue entry)
    again = judge(w, "M1002", s2_verdict(w), [refund(w.s2, 249500)], case_id="CASE-0002").actions[0]
    assert again.decision == Decision.BLOCK and "awaiting approval" in chk(again, "idempotency").detail

    rows_before = scalar(w, "SELECT COUNT(*) FROM actions")
    ok = approve(w.conn, q["action_id"], "ops.lead")
    assert ok.status == "EXECUTED" and ok.amount_paise == 249500 and len(ok.ledger_entry_ids) == 5
    assert scalar(w, "SELECT COUNT(*) FROM actions") == rows_before        # same row, updated
    row = one(w.conn, "SELECT * FROM actions WHERE id = ?", (q["action_id"],))
    assert row["status"] == "EXECUTED" and row["decided_by"] == "human:ops.lead" and row["executed_at"]
    fw = json.loads(row["firewall_json"])
    assert {c["id"]: c["status"] for c in fw["checks"]}["limits"] == "PASS"
    assert fw["checks"][-1]["id"] == "human_approval" and fw["checks"][-1]["status"] == "PASS"
    assert one(w.conn, "SELECT status FROM cases WHERE id = 'CASE-0002'")["status"] == "RESOLVED"

    twice = approve(w.conn, q["action_id"], "ops.lead")
    assert twice.status == "DUPLICATE" and twice.ledger_entry_ids == []
    assert sum(c["amount_paise"] for c in refund_credits(w, "M1002")) == 249500
    assert pending(w.conn) == []


def test_approval_is_blocked_if_refunded_in_the_meantime(db):
    w = db
    _, res = _s2_pending(w)
    aid = res[0].action_id
    _manual_refund(w.conn, "M1002", w.s2[0])
    out = approve(w.conn, aid, "ops.lead")
    assert out.status == "BLOCKED" and out.amount_paise == 0 and "already refunded" in out.detail
    assert len(refund_credits(w, "M1002")) == 1                    # only the manual one: no double payment
    row = one(w.conn, "SELECT * FROM actions WHERE id = ?", (aid,))
    assert row["status"] == "BLOCKED" and row["idempotency_key"] is None
    assert one(w.conn, "SELECT status FROM cases WHERE id = 'CASE-0002'")["status"] == "RESOLVED"   # the cancel still ran


def test_reject_marks_row_and_settles_case(db):
    w = db
    _case(w.conn, "CASE-0003", "M1002", status="AWAITING_APPROVAL")
    _, res = run(w, "M1002", s2_verdict(w), [refund(w.s2, 249500)], case_id="CASE-0003")
    aid = res[0].action_id
    out = reject(w.conn, aid, "ops.lead", "Merchant withdrew the complaint")
    assert out.status == "REJECTED" and "withdrew" in out.detail
    row = one(w.conn, "SELECT * FROM actions WHERE id = ?", (aid,))
    assert row["status"] == "REJECTED" and row["idempotency_key"] is None and row["decided_by"] == "human:ops.lead"
    assert one(w.conn, "SELECT status FROM cases WHERE id = 'CASE-0003'")["status"] == "RESOLVED"
    assert approve(w.conn, aid).status == "REJECTED" and refund_credits(w, "M1002") == []
    with pytest.raises(LookupError):
        approve(w.conn, 999999)


# ───────────────────────────── holds + KYC ─────────────────────────────

def test_risk_hold_is_never_released(db):
    w = db
    v = Verdict(outcome="HOLD_EXPLAINED", culprits=["RISK_HOLD"], headline="Risk hold.", targets={"hold_id": "HLD-1005-01"},
                remedies=[A.RELEASE_HOLD, A.ESCALATE])          # even a verdict that (wrongly) permits release
    act = ProposedAction(action_type=A.RELEASE_HOLD, target_id="HLD-1005-01", amount_paise=300000)
    for human in (False, True):
        av = judge(w, "M1005", v, [act], human_approved=human).actions[0]
        assert av.decision == Decision.BLOCK and chk(av, "hard_deny").status == "FAIL" and "RISK" in chk(av, "hard_deny").detail
    _, res = run(w, "M1005", v, [act])
    assert res[0].status == "BLOCKED"
    # defence in depth: a forged ALLOW verdict still cannot make the executor release it
    forged = FirewallResult(decision=Decision.ALLOW, actions=[ActionVerdict(proposed=act, final=act, decision=Decision.ALLOW, checks=[],
                                                                             idempotency_key="forged-risk-0001")])
    res = record_and_execute(w.conn, case_id=None, merchant_id="M1005", firewall=forged, verdict=v)
    assert res[0].status == "FAILED" and "RISK" in res[0].detail
    assert one(w.conn, "SELECT status FROM holds WHERE id = 'HLD-1005-01'")["status"] == "ACTIVE"
    assert one(w.conn, "SELECT status FROM settlements WHERE id = 'STL-1005-20261111'")["status"] == "HELD"
    assert scalar(w, "SELECT COUNT(*) FROM ledger_entries WHERE merchant_id = 'M1005'") == 0


def test_kyc_hold_with_expired_doc_blocks_release_but_sends_link(db):
    w = db
    _case(w.conn, "CASE-0006", "M1006")
    v = Verdict(outcome="HOLD_EXPLAINED", culprits=["KYC_HOLD"], headline="Drug licence expired.", targets={"hold_id": "HLD-1006-01"},
                remedies=[A.SEND_KYC_LINK, A.RELEASE_HOLD])
    fr, res = run(w, "M1006", v, [ProposedAction(action_type=A.RELEASE_HOLD, target_id="HLD-1006-01", amount_paise=120000),
                                  ProposedAction(action_type=A.SEND_KYC_LINK, target_id="HLD-1006-01")], case_id="CASE-0006")
    rel, link = fr.actions
    assert rel.decision == Decision.BLOCK
    pre = chk(rel, "preconditions")
    assert pre.status == "FAIL" and "DRUG_LICENSE" in pre.detail and "31 Oct 2026" in pre.detail
    assert link.decision == Decision.ALLOW
    assert [r.status for r in res] == ["BLOCKED", "EXECUTED"]
    note = one(w.conn, "SELECT * FROM notifications WHERE merchant_id = 'M1006'")
    assert note and note["case_id"] == "CASE-0006" and "https://paytm.example/kyc/M1006" in note["body"]
    assert one(w.conn, "SELECT status FROM holds WHERE id = 'HLD-1006-01'")["status"] == "ACTIVE"


def test_kyc_hold_release_when_documents_are_valid(db):
    w = db
    v = Verdict(outcome="HOLD_EXPLAINED", culprits=["KYC_HOLD"], headline="Licence renewed.", targets={"hold_id": "HLD-1009-01"},
                remedies=[A.RELEASE_HOLD])
    fr, res = run(w, "M1009", v, [ProposedAction(action_type=A.RELEASE_HOLD, target_id="HLD-1009-01")])
    av = fr.actions[0]
    assert av.decision == Decision.CORRECTED and av.final.amount_paise == 150000     # ₹800 + ₹700, not the settled batch
    assert res[0].status == "EXECUTED" and len(res[0].ledger_entry_ids) == 2
    assert one(w.conn, "SELECT status FROM holds WHERE id = 'HLD-1009-01'")["status"] == "RELEASED"
    st = {r["id"]: r["status"] for r in rows(w.conn, "SELECT id, status FROM settlements WHERE merchant_id = 'M1009'")}
    assert st == {"STL-1009-20261111": "SETTLED", "STL-1009-20261112": "RELEASED", "STL-1009-20261113": "RELEASED"}
    assert scalar(w, "SELECT SUM(amount_paise) FROM ledger_entries WHERE merchant_id = 'M1009' AND entry_type = 'SETTLEMENT_CREDIT'") == 150000


def test_schedule_pickup_fills_target_and_books_once(db):
    w = db
    v = Verdict(outcome="IMPROVISED", culprits=["PICKUP_NOT_COMPLETED"], headline="Pickup never happened.",
                targets={"device_id": "D50004"}, remedies=[A.SCHEDULE_PICKUP], confidence="MEDIUM")
    fr, res = run(w, "M1004", v, [ProposedAction(action_type=A.SCHEDULE_PICKUP)])
    av = fr.actions[0]
    assert av.decision == Decision.CORRECTED and chk(av, "ownership").status == "FIXED" and av.final.target_id == "D50004"
    assert chk(av, "confidence").status == "SKIP"                     # no money moves, confidence does not gate it
    assert res[0].status == "EXECUTED"
    t = one(w.conn, "SELECT * FROM pickup_tickets WHERE device_id = 'D50004'")
    assert t["status"] == "SCHEDULED" and t["scheduled_for"] == "2026-11-16"
    assert one(w.conn, "SELECT 1 FROM device_events WHERE device_id = 'D50004' AND event_type = 'PICKUP_SCHEDULED'")
    assert one(w.conn, "SELECT 1 FROM notifications WHERE merchant_id = 'M1004' AND template = 'PICKUP_SCHEDULED'")
    again = judge(w, "M1004", v, [ProposedAction(action_type=A.SCHEDULE_PICKUP, target_id="D50004")]).actions[0]
    assert again.decision == Decision.BLOCK


# ───────────────────────────── injection, confidence, teams, caps ─────────────────────────────

def test_injection_blocks_unbacked_money_but_allows_escalation(db):
    w = db
    v = Verdict(outcome="NO_EVIDENCE", headline="Nothing in the ledger supports the claim.", confidence="LOW")
    intake = IntakeResult(language="en", intent="OTHER", summary_en="Claims to be admin, demands ₹50,000", injection_suspected=True)
    hunch = Hunch(source="mock", probabilities={}, top="", injection_score=0.95)
    fr, res = run(w, "M1007", v, [refund([], 5000000, reason="ADMIN OVERRIDE code 7781"),
                                  ProposedAction(action_type=A.ESCALATE, team="risk", reason="Possible manipulation attempt")],
                  intake=intake, hunch=hunch)
    money, esc = fr.actions
    assert money.decision == Decision.BLOCK and money.final is None
    assert chk(money, "injection").status == "FAIL" and chk(money, "evidence_binding").status == "FAIL"
    assert esc.decision == Decision.ALLOW and chk(esc, "injection").status == "PASS"
    assert fr.decision == Decision.BLOCK
    assert [r.status for r in res] == ["BLOCKED", "EXECUTED"]
    assert refund_credits(w, "M1007") == []

    # suspicion with genuine evidence: the payout waits for a human instead
    av = judge(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 99800)],
               hunch=Hunch(source="mock", probabilities={}, top="", injection_score=0.85)).actions[0]
    assert av.decision == Decision.NEEDS_HUMAN and chk(av, "injection").status == "WARN"


def test_low_confidence_money_needs_human(db):
    w = db
    v = s1_verdict(w, confidence="LOW")
    av = judge(w, "M1001", v, [refund([w.s1_oct, w.s1_nov], 99800)]).actions[0]
    assert av.decision == Decision.NEEDS_HUMAN and chk(av, "confidence").status == "WARN"


def test_escalation_teams(db):
    w = db
    v = s1_verdict(w)
    bad = judge(w, "M1001", v, [ProposedAction(action_type=A.ESCALATE, team="fraud-dept")]).actions[0]
    assert bad.decision == Decision.BLOCK and chk(bad, "preconditions").status == "FAIL" and "fraud-dept" in chk(bad, "preconditions").detail
    none = judge(w, "M1001", v, [ProposedAction(action_type=A.ESCALATE)]).actions[0]
    assert none.decision == Decision.BLOCK
    ok = judge(w, "M1001", v, [ProposedAction(action_type=A.ESCALATE, team=" Billing ")]).actions[0]
    assert ok.decision == Decision.ALLOW and ok.final.team == "billing"


def test_merchant_30_day_cap_needs_human(db):
    w = db
    v = Verdict(outcome="REFUND_DUE", culprits=["ZOMBIE_BILLING"], headline="Billed after return.", damages_ledger_ids=w.cap,
                damages_paise=99800, remedies=[A.REFUND_DEBITS], confidence="HIGH")
    av = judge(w, "M3001", v, [refund(w.cap, 99800)]).actions[0]
    lim = chk(av, "limits")
    assert av.decision == Decision.NEEDS_HUMAN and lim.status == "WARN"
    assert "₹5,498" in lim.detail and "₹5,000" in lim.detail                 # ₹4,500 earlier + ₹998 now vs the ₹5,000 cap
    # refunds a human approved do not count toward the automatic cap
    w.conn.execute("UPDATE actions SET decided_by = 'human:ops.lead' WHERE id = ?", (w.cap_action,))
    assert judge(w, "M3001", v, [refund(w.cap, 99800)]).actions[0].decision == Decision.ALLOW


# ───────────────────────────── executor safety net ─────────────────────────────

def test_db_guard_reports_duplicate_and_isolates_with_savepoints(db):
    w = db
    _case(w.conn, "CASE-0001", "M1001")
    run(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 99800)], case_id="CASE-0001")
    ledger_before = scalar(w, "SELECT COUNT(*) FROM ledger_entries")
    # a forged result that skips the firewall: the UNIQUE refund_of guard still stops the second payment
    again = refund([w.s1_oct], 49900)
    esc = ProposedAction(action_type=A.ESCALATE, team="billing")
    forged = FirewallResult(decision=Decision.ALLOW, actions=[
        ActionVerdict(proposed=again, final=again, decision=Decision.ALLOW, checks=[], idempotency_key="forged-0000001"),
        ActionVerdict(proposed=esc, final=esc, decision=Decision.ALLOW, checks=[], idempotency_key="forged-0000002"),
    ])
    res = record_and_execute(w.conn, case_id="CASE-0001", merchant_id="M1001", firewall=forged, verdict=s1_verdict(w))
    assert [r.status for r in res] == ["DUPLICATE", "EXECUTED"]
    assert scalar(w, "SELECT COUNT(*) FROM ledger_entries") == ledger_before
    # the same idempotency key twice → DUPLICATE, not an exception
    res2 = record_and_execute(w.conn, case_id="CASE-0001", merchant_id="M1001",
                              firewall=FirewallResult(decision=Decision.ALLOW, actions=[forged.actions[1]]), verdict=s1_verdict(w))
    assert res2[0].status == "DUPLICATE"
    # nothing was committed: the caller's rollback undoes every write
    w.conn.rollback()
    assert scalar(w, "SELECT COUNT(*) FROM actions WHERE case_id IS NOT NULL") == 0
    assert refund_credits(w, "M1001") == []


# ───────────────────────────── fleet sweep ─────────────────────────────

def test_sweep_queues_approves_and_skips_refunded_victims(db):
    w = db
    _case(w.conn, "CASE-0001", "M1001")
    victims = [sweep_victim(w, m) for m in ("M2001", "M2002", "M2003")] + [sweep_victim(w, "M2004", damages_paise=0)]
    res = create_sweep(w.conn, source_case_id="CASE-0001", suspect_id="ZOMBIE_BILLING", victims=victims)
    assert res.sweep_id == "SWP-0001" and res.merchant_count == 3 and res.total_paise == 49900 * 6
    assert [v.merchant_id for v in res.victims] == ["M2001", "M2002", "M2003"]
    assert {v.merchant_id: v.damages_paise for v in res.victims} == {"M2001": 49900, "M2002": 99800, "M2003": 149700}
    queued = [p for p in pending(w.conn) if p["sweep_id"] == res.sweep_id]
    assert len(queued) >= 3 and {p["action_type"] for p in queued} == {"REFUND_DEBITS", "CANCEL_SUBSCRIPTION"}
    assert all(any(c["id"] == "sweep_batch" and c["status"] == "WARN" for c in p["checks"]) for p in queued)
    assert all(p["complaint_text"] for p in queued)              # the source case's complaint, for context
    sw = one(w.conn, "SELECT * FROM sweeps WHERE id = 'SWP-0001'")
    assert sw["status"] == "PENDING" and sw["merchant_count"] == 3 and sw["total_paise"] == 299400
    m = metrics(w.conn)
    assert m["sweeps"] == 1 and m["sweep_pending_paise"] == 299400
    assert refund_credits(w, "M2001") == []

    # the same sweep again finds nothing new to queue
    assert create_sweep(w.conn, source_case_id="CASE-0001", suspect_id="ZOMBIE_BILLING", victims=victims).sweep_id is None

    # one victim gets refunded by hand before the ops lead signs off
    _manual_refund(w.conn, "M2003", w.victims["M2003"][0])
    out = approve_sweep(w.conn, "SWP-0001", "ops.lead")
    assert len(out) == len(queued)
    by = {}
    for p, r in zip(queued, out):
        by[(p["merchant_id"], p["action_type"])] = r.status
    assert by[("M2003", "REFUND_DEBITS")] == "BLOCKED"
    assert by[("M2001", "REFUND_DEBITS")] == by[("M2002", "REFUND_DEBITS")] == "EXECUTED"
    assert all(by[(mid, "CANCEL_SUBSCRIPTION")] == "EXECUTED" for mid in ("M2001", "M2002", "M2003"))
    paid = sum(r.amount_paise for r in out if r.status == "EXECUTED" and r.action_type == A.REFUND_DEBITS)
    assert paid == 149700
    assert scalar(w, "SELECT SUM(amount_paise) FROM ledger_entries WHERE entry_type = 'REFUND_CREDIT' AND source_type = 'action' "
                     "AND merchant_id IN ('M2001','M2002','M2003')") == paid
    assert len(refund_credits(w, "M2003")) == 1                     # only the manual one
    assert one(w.conn, "SELECT status FROM sweeps WHERE id = 'SWP-0001'")["status"] == "APPROVED"
    assert metrics(w.conn)["sweep_pending_paise"] == 0
    assert approve_sweep(w.conn, "SWP-0001") == []
    with pytest.raises(LookupError):
        approve_sweep(w.conn, "SWP-9999")


def test_sweep_with_no_qualifying_victims_creates_nothing(db):
    w = db
    _case(w.conn, "CASE-0001", "M1001")
    res = create_sweep(w.conn, source_case_id="CASE-0001", suspect_id="ZOMBIE_BILLING", victims=[sweep_victim(w, "M2004", damages_paise=0)])
    assert res.sweep_id is None and res.merchant_count == 0 and res.victims == []
    assert scalar(w, "SELECT COUNT(*) FROM sweeps") == 0


# ───────────────────────────── metrics ─────────────────────────────

def test_metrics_are_consistent(db):
    w = db
    base = metrics(w.conn)["refunded_paise"]          # the seed's earlier ₹4,500 auto refund (M3001)
    assert base == 450000
    _case(w.conn, "CASE-0001", "M1001")
    run(w, "M1001", s1_verdict(w), [refund([w.s1_oct, w.s1_nov], 149700),
                                    ProposedAction(action_type=A.CANCEL_SUBSCRIPTION, target_id="SUB-1001-A")], case_id="CASE-0001")
    w.conn.execute("UPDATE cases SET status = 'RESOLVED' WHERE id = 'CASE-0001'")     # the runner's job
    _, s2 = _s2_pending(w)
    _case(w.conn, "CASE-0005", "M1005", status="ESCALATED")
    run(w, "M1005", Verdict(outcome="HOLD_EXPLAINED", headline="Risk hold.", remedies=[A.ESCALATE]),
        [ProposedAction(action_type=A.RELEASE_HOLD, target_id="HLD-1005-01")], case_id="CASE-0005")

    m = metrics(w.conn)
    assert m["cases"] == 3
    assert m["resolved_auto"] == 1
    assert m["awaiting_approval"] == len(pending(w.conn)) == 1
    assert m["blocked_actions"] == 1
    assert m["refunded_paise"] == base + 99800
    assert m["corrected_actions"] == 1 and m["prevented_paise"] >= 49900
    assert m["sweeps"] == 0 and m["sweep_pending_paise"] == 0

    approve(w.conn, s2[0].action_id, "ops.lead")
    m2 = metrics(w.conn)
    assert m2["refunded_paise"] == base + 99800 + 249500
    assert m2["refunded_paise"] == scalar(w, "SELECT SUM(amount_paise) FROM ledger_entries WHERE entry_type = 'REFUND_CREDIT' AND source_type = 'action'")
    assert m2["awaiting_approval"] == 0 and m2["resolved_auto"] == 1            # CASE-0002 was resolved by a human
    assert m2["human_approved_actions"] == 1
