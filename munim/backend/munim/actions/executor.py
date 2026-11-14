"""Executor: records every firewall decision as an `actions` row and applies the effects of allowed actions.

The caller owns the transaction (`with munim.db.conn.tx() as conn:`); nothing here commits. Each action runs inside
its own SAVEPOINT, so one DB-level refusal (UNIQUE refund_of / idempotency_key) rolls back only that action and is
reported as `DUPLICATE`, while the other actions of the same proposal still go through.

    ALLOW / CORRECTED → EXECUTED row + effects      NEEDS_HUMAN → PENDING_APPROVAL row, no effects
    BLOCK             → BLOCKED row, key NULL, no effects

`approve()` re-runs the firewall on the stored final params with human_approved=True (hard denies and idempotency still
apply) and updates the same row. `ExecutionResult.amount_paise` is the final amount for EXECUTED / PENDING_APPROVAL /
REJECTED and 0 when nothing could move (BLOCKED / DUPLICATE / FAILED).
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from munim import config
from munim.db.conn import audit, now_iso, one, rows
from munim.firewall.policy import (
    MONEY_ACTIONS,
    describe_debits,
    evaluate,
    fmt_date,
    held_settlements_for_hold,
    join_human,
    make_check,
    plural,
    team_name,
)
from munim.models import (
    ActionType,
    ActionVerdict,
    Decision,
    ExecutionResult,
    FirewallResult,
    Proposal,
    ProposedAction,
    Verdict,
    rupees,
)

KYC_LINK_BASE = "https://paytm.example/kyc"
DOC_NAMES = {"DRUG_LICENSE": "drug licence", "SHOP_LICENSE": "shop licence", "PAN": "PAN", "AADHAAR": "Aadhaar", "GSTIN": "GSTIN"}
_SP = itertools.count(1)


class EffectError(Exception):
    """The world changed between the firewall's look and the write (e.g. subscription no longer ACTIVE)."""


# ───────────────────────────── plumbing ─────────────────────────────

@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """Nested unit of work. Opens the caller's transaction if none is open (never commits it)."""
    name = f"munim_sp_{next(_SP)}"
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _action_type(av: ActionVerdict) -> tuple[ActionType, str]:
    a = av.final or av.proposed
    raw = str(getattr(a.action_type, "value", a.action_type))
    try:
        return ActionType(raw), raw
    except ValueError:
        return ActionType.NO_ACTION, raw


def _first(av: ActionVerdict, status: str) -> str:
    for c in av.checks:
        if c.status == status:
            return c.detail
    return ""


# the most telling reason first: "already refunded" beats "wrong verdict", a risk hold beats "not a permitted remedy"
_BLOCK_PRIORITY = ("idempotency", "hard_deny", "injection", "ownership", "allowlist")


def _block_reason(av: ActionVerdict) -> str:
    fails = {c.id: c.detail for c in av.checks if c.status == "FAIL"}
    for cid in _BLOCK_PRIORITY:
        if cid in fails:
            return fails[cid]
    return next(iter(fails.values()), "")


def _existing_case(conn: sqlite3.Connection, case_id: str | None) -> str | None:
    if case_id and one(conn, "SELECT 1 FROM cases WHERE id = ?", (case_id,)):
        return case_id
    return None


def _sweep_source(conn: sqlite3.Connection, sweep_id: str | None) -> str | None:
    if not sweep_id:
        return None
    r = one(conn, "SELECT source_case_id FROM sweeps WHERE id = ?", (sweep_id,))
    return r["source_case_id"] if r else None


def _merchant(conn: sqlite3.Connection, merchant_id: str) -> dict:
    return one(conn, "SELECT id, business_name, city, language FROM merchants WHERE id = ?", (merchant_id,)) or {
        "id": merchant_id, "business_name": merchant_id, "city": "", "language": "en"}


def _stamp(today: str) -> str:
    return today.replace("-", "")[:8]


def _posted(today: str) -> str:
    # business date + wall-clock time of day, so same-day rows keep their order in the passbook
    return f"{today}T{datetime.now():%H:%M:%S}"


def _insert_action(conn: sqlite3.Connection, *, case_ref: str | None, sweep_id: str | None, merchant_id: str, action_type: str,
                   amount: int, params: str, proposed: str, firewall: str, verdict: str, key: str | None, decision: str,
                   status: str, decided_by: str) -> int:
    cur = conn.execute(
        """INSERT INTO actions(case_id, sweep_id, merchant_id, action_type, amount_paise, params_json, proposed_json, firewall_json,
                               verdict_json, idempotency_key, decision, status, decided_by, created_at, executed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (case_ref, sweep_id, merchant_id, action_type, amount, params, proposed, firewall, verdict, key, decision, status,
         decided_by, now_iso(), now_iso() if status == "EXECUTED" else None),
    )
    return int(cur.lastrowid)


def _notify(conn: sqlite3.Connection, merchant_id: str, case_ref: str | None, channel: str, template: str, body: str) -> int:
    cur = conn.execute(
        "INSERT INTO notifications(merchant_id, case_id, channel, template, body, sent_at) VALUES (?,?,?,?,?,?)",
        (merchant_id, case_ref, channel, template, body, now_iso()),
    )
    return int(cur.lastrowid)


def _dup_detail(err: sqlite3.IntegrityError) -> str:
    msg = str(err)
    if "refund_of" in msg:
        return "The database refused a second refund for the same debit (refund guard), so nothing was paid twice."
    if "idempotency_key" in msg:
        return "An identical action is already recorded (same idempotency key), so this one was not run again."
    return f"The database refused a duplicate record ({msg})."


# ───────────────────────────── effects ─────────────────────────────

def _apply(conn: sqlite3.Connection, action_id: int, a: ProposedAction, *, merchant: dict, case_ref: str | None, ref: str,
           today: str) -> tuple[str, list[int]]:
    """Apply one allowed action. Returns (human detail, ledger ids created). Raises EffectError / sqlite3.Error."""
    t, m, name = ActionType(a.action_type), merchant["id"], merchant["business_name"]

    if t == ActionType.REFUND_DEBITS:
        ids = list(dict.fromkeys(a.ledger_entry_ids))
        if not ids:
            raise EffectError("there are no ledger entries to refund")
        found = {r["id"]: r for r in rows(conn, f"SELECT * FROM ledger_entries WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
        created: list[int] = []
        originals: list[dict] = []
        for n, lid in enumerate(ids, 1):
            o = found.get(lid)
            if not o or o["merchant_id"] != m or o["direction"] != "DEBIT" or o["status"] != "SUCCESS":
                raise EffectError(f"ledger entry {lid} is not a successful debit of {m}")
            cur = conn.execute(
                """INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, source_type, source_id,
                                              mandate_id, value_date, posted_at, narration, refund_of)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m, f"RFD{_stamp(today)}M{action_id:06d}{n:02d}", "REFUND_CREDIT", "CREDIT", o["amount_paise"], "SUCCESS", "action",
                 str(action_id), None, today, _posted(today), f"Refund of {o['narration']} · Munim {ref}", lid),
            )
            created.append(int(cur.lastrowid))
            originals.append(o)
        total = sum(int(o["amount_paise"]) for o in originals)
        if total != a.amount_paise:
            raise EffectError(f"the ledger now sums to {rupees(total)}, not the approved {rupees(a.amount_paise)}")
        return (f"Refunded {rupees(total)} to {name} as {plural(len(created), 'credit')} for {describe_debits(originals)}.", created)

    if t == ActionType.CANCEL_SUBSCRIPTION:
        sub = one(conn, "SELECT * FROM subscriptions WHERE id = ? AND merchant_id = ?", (a.target_id, m))
        if not sub or sub["status"] != "ACTIVE":
            raise EffectError(f"subscription {a.target_id} is no longer ACTIVE")
        reason = f"Munim {ref}: {a.reason or 'billing stopped after a verified fault'}"[:200]
        conn.execute("UPDATE subscriptions SET status = 'CANCELLED', ended_at = ?, end_reason = ? WHERE id = ?", (today, reason, sub["id"]))
        mandates = [r["id"] for r in rows(conn, "SELECT id FROM mandates WHERE subscription_id = ? AND status = 'ACTIVE'", (sub["id"],))]
        conn.execute("UPDATE mandates SET status = 'REVOKED' WHERE subscription_id = ? AND status = 'ACTIVE'", (sub["id"],))
        revoked = f" and revoked mandate {join_human(mandates)}" if mandates else ""
        return (f"Cancelled subscription {sub['id']} from {fmt_date(today)}{revoked}, so no further rent will be charged.", [])

    if t == ActionType.SCHEDULE_PICKUP:
        dev = one(conn, "SELECT * FROM devices WHERE id = ?", (a.target_id,))
        if not dev or dev["status"] == "RETURNED":
            raise EffectError(f"device {a.target_id} is already returned")
        when = (date.fromisoformat(today[:10]) + timedelta(days=2)).isoformat()
        ticket = f"PK-{_stamp(today)[2:]}-M{action_id:04d}"
        conn.execute(
            "INSERT INTO pickup_tickets(id, device_id, merchant_id, status, requested_at, scheduled_for, attempts) VALUES (?,?,?,?,?,?,0)",
            (ticket, dev["id"], m, "SCHEDULED", today, when),
        )
        conn.execute("INSERT INTO device_events(device_id, event_type, at, ref, note) VALUES (?,?,?,?,?)",
                     (dev["id"], "PICKUP_SCHEDULED", today, ticket, f"Booked by Munim ({ref})"))
        _notify(conn, m, case_ref, "sms", "PICKUP_SCHEDULED",
                f"Your Soundbox {dev['id']} pickup is booked for {fmt_date(when)} (ticket {ticket}). Please keep the device and charger ready.")
        return (f"Booked pickup {ticket} for device {dev['id']} on {fmt_date(when)} and sent the merchant an SMS.", [])

    if t == ActionType.SEND_KYC_LINK:
        link = f"{KYC_LINK_BASE}/{m}"
        docs = rows(conn, "SELECT doc_type FROM kyc_documents WHERE merchant_id = ? AND status IN ('EXPIRED','PENDING') ORDER BY doc_type", (m,))
        what = join_human(DOC_NAMES.get(d["doc_type"], d["doc_type"].lower().replace("_", " ")) for d in docs) or "KYC documents"
        _notify(conn, m, case_ref, "sms", "KYC_LINK",
                f"Please update your {what} to release your held settlements: {link}")
        return (f"Sent a re-KYC link ({link}) by SMS to update the {what}.", [])

    if t == ActionType.RELEASE_HOLD:
        hold = one(conn, "SELECT * FROM holds WHERE id = ? AND merchant_id = ?", (a.target_id, m))
        if not hold or hold["status"] != "ACTIVE":
            raise EffectError(f"hold {a.target_id} is no longer ACTIVE")
        if str(hold["hold_type"]).upper() == "RISK":                       # defence in depth: never, whatever the caller says
            raise EffectError(f"hold {hold['id']} is a RISK hold")
        held = held_settlements_for_hold(conn, hold)
        conn.execute("UPDATE holds SET status = 'RELEASED', released_at = ? WHERE id = ?", (today, hold["id"]))
        created = []
        for n, s in enumerate(held, 1):
            conn.execute("UPDATE settlements SET status = 'RELEASED', utr = COALESCE(utr, ?), settled_at = ? WHERE id = ?",
                         (f"UTR{_stamp(today)}M{action_id:06d}{n:02d}", today, s["id"]))
            if int(s["net_paise"]) > 0:
                cur = conn.execute(
                    """INSERT INTO ledger_entries(merchant_id, txn_ref, entry_type, direction, amount_paise, status, source_type, source_id,
                                                  mandate_id, value_date, posted_at, narration, refund_of)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (m, f"STL{_stamp(today)}M{action_id:06d}{n:02d}", "SETTLEMENT_CREDIT", "CREDIT", s["net_paise"], "SUCCESS", "settlement",
                     s["id"], None, today, _posted(today), f"Settlement {fmt_date(s['batch_date'])} released (hold {hold['id']}) · Munim {ref}", None),
                )
                created.append(int(cur.lastrowid))
        total = sum(int(s["net_paise"]) for s in held)
        if total != a.amount_paise:
            raise EffectError(f"the held settlements now total {rupees(total)}, not the approved {rupees(a.amount_paise)}")
        return (f"Released hold {hold['id']} and paid out {rupees(total)} from {plural(len(held), 'held settlement')}.", created)

    if t == ActionType.ESCALATE:
        why = f": {a.reason}" if a.reason else ""
        return (f"Escalated to the {team_name(a.team)} team with the full case file{why}"[:400].rstrip(".") + ".", [])

    why = f": {a.reason}" if a.reason else ""
    return (f"No action taken{why}"[:400].rstrip(".") + ".", [])


# ───────────────────────────── record + execute ─────────────────────────────

def _record_one(conn: sqlite3.Connection, av: ActionVerdict, *, case_ref: str | None, audit_case: str | None, merchant: dict,
                verdict_json: str, sweep_id: str | None, ref: str, today: str) -> ExecutionResult:
    t, raw_type = _action_type(av)
    final = av.final
    if av.decision == Decision.BLOCK or final is None:
        status = "BLOCKED"
    elif av.decision == Decision.NEEDS_HUMAN:
        status = "PENDING_APPROVAL"
    else:
        status = "EXECUTED"
    amount = final.amount_paise if final is not None and status != "BLOCKED" else 0
    fields = dict(case_ref=case_ref, sweep_id=sweep_id, merchant_id=merchant["id"], action_type=raw_type, amount=amount,
                  params=(final or av.proposed).model_dump_json(warnings=False), proposed=av.proposed.model_dump_json(warnings=False),
                  firewall=av.model_dump_json(warnings=False), verdict=verdict_json)
    summary = {"action_type": raw_type, "decision": str(av.decision), "checks": {c.id: c.status for c in av.checks}}
    try:
        with _savepoint(conn):
            aid = _insert_action(conn, **fields, key=None if status == "BLOCKED" else av.idempotency_key, decision=str(av.decision),
                                 status=status, decided_by="firewall")
            created: list[int] = []
            if status == "EXECUTED":
                detail, created = _apply(conn, aid, final, merchant=merchant, case_ref=case_ref, ref=ref, today=today)
            elif status == "PENDING_APPROVAL":
                detail = f"Waiting for human approval: {_first(av, 'WARN') or 'the firewall asked for a human'}"
            else:
                detail = f"Blocked by the firewall: {_block_reason(av) or 'a check failed'}"
            audit(conn, audit_case, "firewall", "action_decision", {"action_id": aid, **summary})
            audit(conn, audit_case, "executor", f"action_{status.lower()}",
                  {"action_id": aid, "action_type": raw_type, "amount_paise": amount, "detail": detail, "ledger_entry_ids": created})
        return ExecutionResult(action_id=aid, action_type=t, status=status, amount_paise=amount, detail=detail, ledger_entry_ids=created)
    except sqlite3.IntegrityError as e:
        unique = "UNIQUE" in str(e)
        return _fallback(conn, fields, summary, audit_case, t, "DUPLICATE" if unique else "FAILED",
                         _dup_detail(e) if unique else f"Could not record {raw_type}: {e}.")
    except Exception as e:  # noqa: BLE001 - the demo must never crash; the failure is recorded and audited
        return _fallback(conn, fields, summary, audit_case, t, "FAILED", f"Could not execute {raw_type}: {e}.")


def _fallback(conn: sqlite3.Connection, fields: dict, summary: dict, audit_case: str | None, t: ActionType,
              result_status: str, detail: str) -> ExecutionResult:
    """Record an action whose effects were rolled back (key NULL so it can't collide again)."""
    row_status = "BLOCKED" if result_status == "DUPLICATE" else "FAILED"
    aid: int | None = None
    try:
        with _savepoint(conn):
            aid = _insert_action(conn, **{**fields, "amount": 0}, key=None,
                                 decision="BLOCK" if row_status == "BLOCKED" else summary["decision"],
                                 status=row_status, decided_by="executor")
            audit(conn, audit_case, "executor", f"action_{result_status.lower()}",
                  {"action_id": aid, "action_type": summary["action_type"], "detail": detail})
    except sqlite3.Error:
        aid = None
    return ExecutionResult(action_id=aid, action_type=t, status=result_status, amount_paise=0, detail=detail)


def record_and_execute(conn: sqlite3.Connection, *, case_id: str | None, merchant_id: str, firewall: FirewallResult,
                       verdict: Verdict, sweep_id: str | None = None, today: str = config.DEMO_TODAY) -> list[ExecutionResult]:
    """Insert one `actions` row per ActionVerdict and apply the effects of ALLOW / CORRECTED ones. Never commits."""
    case_ref = _existing_case(conn, case_id)
    audit_case = case_id or _sweep_source(conn, sweep_id)
    merchant = _merchant(conn, merchant_id)
    verdict_json = verdict.model_dump_json(warnings=False)
    ref = case_id or sweep_id or "ops"
    return [
        _record_one(conn, av, case_ref=case_ref, audit_case=audit_case, merchant=merchant, verdict_json=verdict_json,
                    sweep_id=sweep_id, ref=ref, today=today)
        for av in firewall.actions
    ]


# ───────────────────────────── approvals ─────────────────────────────

def _load(conn: sqlite3.Connection, action_id: int) -> dict:
    row = one(conn, "SELECT * FROM actions WHERE id = ?", (action_id,))
    if not row:
        raise LookupError(f"action {action_id} not found")
    return row


def _row_type(row: dict) -> ActionType:
    try:
        return ActionType(row["action_type"])
    except ValueError:
        return ActionType.NO_ACTION


def _not_pending(row: dict, verb: str) -> ExecutionResult:
    t, st = _row_type(row), row["status"]
    if st == "EXECUTED":
        if verb == "approve":
            return ExecutionResult(action_id=row["id"], action_type=t, status="DUPLICATE", amount_paise=0,
                                   detail=f"Action #{row['id']} was already executed, so approving it again changes nothing.")
        return ExecutionResult(action_id=row["id"], action_type=t, status="EXECUTED", amount_paise=0,
                               detail=f"Action #{row['id']} was already executed, so it can no longer be rejected.")
    return ExecutionResult(action_id=row["id"], action_type=t, status=st if st in ("REJECTED", "BLOCKED", "FAILED") else "FAILED",
                           amount_paise=0, detail=f"Action #{row['id']} is {st}, so there is nothing to {verb}.")


def _settle_case(conn: sqlite3.Connection, case_id: str | None) -> None:
    """When a case's last pending action is decided: ESCALATED if an escalation ran, RESOLVED if anything ran or a human
    rejected the rest, ESCALATED if everything ended up blocked (someone must follow up)."""
    if not case_id or not one(conn, "SELECT 1 FROM cases WHERE id = ?", (case_id,)):
        return
    acts = rows(conn, "SELECT action_type, status FROM actions WHERE case_id = ?", (case_id,))
    if not acts or any(a["status"] == "PENDING_APPROVAL" for a in acts):
        return
    executed = [a for a in acts if a["status"] == "EXECUTED"]
    refunded = any(a["action_type"] == ActionType.REFUND_DEBITS.value for a in executed)
    if not refunded and any(a["action_type"] == ActionType.ESCALATE.value for a in executed):
        status = "ESCALATED"
    elif executed or any(a["status"] == "REJECTED" for a in acts):
        status = "RESOLVED"
    else:
        status = "ESCALATED"
    conn.execute("UPDATE cases SET status = ?, closed_at = ? WHERE id = ?",
                 (status, now_iso() if status == "RESOLVED" else None, case_id))


def _settle_sweep(conn: sqlite3.Connection, sweep_id: str | None) -> None:
    if not sweep_id:
        return
    acts = rows(conn, "SELECT status FROM actions WHERE sweep_id = ?", (sweep_id,))
    if not acts or any(a["status"] == "PENDING_APPROVAL" for a in acts):
        return
    status = "APPROVED" if any(a["status"] == "EXECUTED" for a in acts) else "REJECTED"
    conn.execute("UPDATE sweeps SET status = ? WHERE id = ?", (status, sweep_id))


def _clean_name(approver: str | None) -> str:
    return (str(approver or "").strip() or "ops.lead")[:64]


def approve(conn: sqlite3.Connection, action_id: int, approver: str = "ops.lead") -> ExecutionResult:
    """Human approval of a PENDING_APPROVAL action: re-evaluate (human_approved=True), then execute or block. Same row."""
    row = _load(conn, action_id)
    approver = _clean_name(approver)
    if row["status"] != "PENDING_APPROVAL":
        return _not_pending(row, "approve")
    t = _row_type(row)
    audit_case = row["case_id"] or _sweep_source(conn, row["sweep_id"])
    merchant = _merchant(conn, row["merchant_id"])
    ref = row["case_id"] or row["sweep_id"] or f"action {action_id}"
    try:
        params = ProposedAction.model_validate_json(row["params_json"])
        verdict = Verdict.model_validate_json(row["verdict_json"] or "")
    except Exception as e:  # noqa: BLE001 - a corrupted row must not be executable
        with _savepoint(conn):
            conn.execute("UPDATE actions SET status = 'FAILED', idempotency_key = NULL, decided_by = ? WHERE id = ?",
                         (f"human:{approver}", action_id))
            audit(conn, audit_case, "executor", "action_failed", {"action_id": action_id, "error": str(e)})
        return ExecutionResult(action_id=action_id, action_type=t, status="FAILED", detail=f"Stored parameters of action #{action_id} are unreadable.")

    fr = evaluate(conn, merchant_id=row["merchant_id"], proposal=Proposal(actions=[params]), verdict=verdict,
                  case_id=row["case_id"], human_approved=True, today=config.DEMO_TODAY)
    av = fr.actions[0]
    av.checks.append(make_check("human_approval", "PASS", f"Approved by {approver} after reviewing the evidence."))
    if row["sweep_id"]:
        av.checks.append(make_check("sweep_batch", "PASS", f"Signed off by {approver} as part of fleet sweep {row['sweep_id']}."))

    result: ExecutionResult
    if av.decision == Decision.BLOCK or av.final is None:
        detail = f"Blocked on approval: {_block_reason(av) or 'a firewall check failed'}"
        with _savepoint(conn):
            conn.execute("UPDATE actions SET status = 'BLOCKED', decision = 'BLOCK', idempotency_key = NULL, decided_by = 'firewall', "
                         "firewall_json = ? WHERE id = ?", (av.model_dump_json(warnings=False), action_id))
            audit(conn, audit_case, "human", "action_approved", {"action_id": action_id, "approver": approver})
            audit(conn, audit_case, "firewall", "action_blocked_on_approval", {"action_id": action_id, "detail": detail})
        result = ExecutionResult(action_id=action_id, action_type=t, status="BLOCKED", amount_paise=0, detail=detail)
    elif av.decision == Decision.NEEDS_HUMAN:  # cannot happen (approval satisfies every WARN), kept for safety
        result = ExecutionResult(action_id=action_id, action_type=t, status="PENDING_APPROVAL", amount_paise=row["amount_paise"],
                                 detail=f"Still waiting for approval: {_first(av, 'WARN')}")
    else:
        final = av.final
        try:
            with _savepoint(conn):
                detail, created = _apply(conn, action_id, final, merchant=merchant, case_ref=row["case_id"], ref=ref, today=config.DEMO_TODAY)
                conn.execute(
                    "UPDATE actions SET status = 'EXECUTED', decided_by = ?, executed_at = ?, params_json = ?, amount_paise = ?, "
                    "firewall_json = ?, idempotency_key = ? WHERE id = ?",
                    (f"human:{approver}", now_iso(), final.model_dump_json(warnings=False), final.amount_paise, av.model_dump_json(warnings=False),
                     av.idempotency_key, action_id),
                )
                audit(conn, audit_case, "human", "action_approved", {"action_id": action_id, "approver": approver})
                audit(conn, audit_case, "executor", "action_executed",
                      {"action_id": action_id, "action_type": t.value, "amount_paise": final.amount_paise, "detail": detail, "ledger_entry_ids": created})
            result = ExecutionResult(action_id=action_id, action_type=t, status="EXECUTED", amount_paise=final.amount_paise,
                                     detail=detail, ledger_entry_ids=created)
        except Exception as e:  # noqa: BLE001
            unique = isinstance(e, sqlite3.IntegrityError) and "UNIQUE" in str(e)
            detail = _dup_detail(e) if unique else f"Could not execute {t.value}: {e}."
            with _savepoint(conn):
                conn.execute("UPDATE actions SET status = ?, decision = ?, idempotency_key = NULL, decided_by = 'executor', firewall_json = ? WHERE id = ?",
                             ("BLOCKED" if unique else "FAILED", "BLOCK" if unique else row["decision"], av.model_dump_json(warnings=False), action_id))
                audit(conn, audit_case, "executor", "action_duplicate" if unique else "action_failed", {"action_id": action_id, "detail": detail})
            result = ExecutionResult(action_id=action_id, action_type=t, status="DUPLICATE" if unique else "FAILED", amount_paise=0, detail=detail)

    _settle_case(conn, row["case_id"])
    _settle_sweep(conn, row["sweep_id"])
    return result


def reject(conn: sqlite3.Connection, action_id: int, approver: str = "ops.lead", reason: str = "") -> ExecutionResult:
    """Human rejection of a PENDING_APPROVAL action. Frees its idempotency key so a later, better case can re-queue it."""
    row = _load(conn, action_id)
    approver = _clean_name(approver)
    if row["status"] != "PENDING_APPROVAL":
        return _not_pending(row, "reject")
    t = _row_type(row)
    reason = str(reason or "").strip()[:300]
    detail = f"Rejected by {approver}" + (f": {reason.rstrip('.')}." if reason else ".")
    audit_case = row["case_id"] or _sweep_source(conn, row["sweep_id"])
    try:
        fw = json.loads(row["firewall_json"] or "{}")
        fw.setdefault("checks", []).append(make_check("human_approval", "FAIL", detail).model_dump())
        fw_json = json.dumps(fw, ensure_ascii=False)
    except (TypeError, ValueError):
        fw_json = row["firewall_json"]
    with _savepoint(conn):
        conn.execute("UPDATE actions SET status = 'REJECTED', idempotency_key = NULL, decided_by = ?, firewall_json = ? WHERE id = ?",
                     (f"human:{approver}", fw_json, action_id))
        audit(conn, audit_case, "human", "action_rejected", {"action_id": action_id, "approver": approver, "reason": reason})
    _settle_case(conn, row["case_id"])
    _settle_sweep(conn, row["sweep_id"])
    return ExecutionResult(action_id=action_id, action_type=t, status="REJECTED", amount_paise=row["amount_paise"], detail=detail)


# ───────────────────────────── read models ─────────────────────────────

def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def pending(conn: sqlite3.Connection) -> list[dict]:
    """PENDING_APPROVAL rows with merchant name, case complaint (or the sweep's source complaint) and firewall checks."""
    out = []
    for r in rows(conn, """
            SELECT a.*, m.business_name, m.city, c.complaint_text AS case_complaint, sc.complaint_text AS sweep_complaint,
                   s.suspect_id AS sweep_suspect
            FROM actions a
            JOIN merchants m ON m.id = a.merchant_id
            LEFT JOIN cases c ON c.id = a.case_id
            LEFT JOIN sweeps s ON s.id = a.sweep_id
            LEFT JOIN cases sc ON sc.id = s.source_case_id
            WHERE a.status = 'PENDING_APPROVAL'
            ORDER BY a.id"""):
        fw = _json(r["firewall_json"], {})
        checks = fw.get("checks", [])
        verdict = _json(r["verdict_json"], {})
        out.append({
            "action_id": r["id"],
            "case_id": r["case_id"],
            "sweep_id": r["sweep_id"],
            "merchant_id": r["merchant_id"],
            "business_name": r["business_name"],
            "city": r["city"],
            "action_type": r["action_type"],
            "amount_paise": r["amount_paise"],
            "params": _json(r["params_json"], {}),
            "proposed": _json(r["proposed_json"], {}),
            "checks": checks,
            "reasons": [c.get("detail") for c in checks if c.get("status") == "WARN"],
            "decision": r["decision"],
            "headline": verdict.get("headline"),
            "complaint_text": r["case_complaint"] or r["sweep_complaint"],
            "sweep_suspect": r["sweep_suspect"],
            "created_at": r["created_at"],
        })
    return out


def metrics(conn: sqlite3.Connection) -> dict:
    """Dashboard numbers. `refunded_paise` is read from the ledger (truth), not from the actions table."""
    def scalar(sql: str, params: tuple = ()) -> int:
        r = conn.execute(sql, params).fetchone()
        return int(r[0] or 0) if r else 0

    money = tuple(t.value for t in MONEY_ACTIONS)
    return {
        "cases": scalar("SELECT COUNT(*) FROM cases"),
        "resolved_auto": scalar("""SELECT COUNT(*) FROM cases c WHERE c.status = 'RESOLVED' AND NOT EXISTS (
                                     SELECT 1 FROM actions a WHERE a.case_id = c.id
                                       AND (a.decided_by LIKE 'human:%' OR a.status IN ('PENDING_APPROVAL', 'REJECTED')))"""),
        "awaiting_approval": scalar("SELECT COUNT(*) FROM actions WHERE status = 'PENDING_APPROVAL'"),
        "blocked_actions": scalar("SELECT COUNT(*) FROM actions WHERE status = 'BLOCKED'"),
        "refunded_paise": scalar("SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE entry_type = 'REFUND_CREDIT' AND source_type = 'action'"),
        "sweeps": scalar("SELECT COUNT(*) FROM sweeps"),
        "sweep_pending_paise": scalar("""SELECT COALESCE(SUM(amount_paise), 0) FROM actions
                                         WHERE sweep_id IS NOT NULL AND status = 'PENDING_APPROVAL' AND action_type = 'REFUND_DEBITS'"""),
        # extras for the dashboard
        "executed_actions": scalar("SELECT COUNT(*) FROM actions WHERE status = 'EXECUTED'"),
        "auto_executed_actions": scalar("SELECT COUNT(*) FROM actions WHERE status = 'EXECUTED' AND decided_by = 'firewall'"),
        "human_approved_actions": scalar("SELECT COUNT(*) FROM actions WHERE status = 'EXECUTED' AND decided_by LIKE 'human:%'"),
        "rejected_actions": scalar("SELECT COUNT(*) FROM actions WHERE status = 'REJECTED'"),
        "corrected_actions": scalar("SELECT COUNT(*) FROM actions WHERE decision = 'CORRECTED'"),
        # money the LLM asked for beyond what the evidence supports (corrections + blocks), capped at ₹10 crore per row
        "prevented_paise": scalar(f"""SELECT COALESCE(SUM(MIN(MAX(COALESCE(CAST(json_extract(proposed_json, '$.amount_paise') AS INTEGER), 0)
                                                           - amount_paise, 0), 1000000000)), 0)
                                      FROM actions WHERE action_type IN ({','.join('?' * len(money))}) AND status <> 'REJECTED'""", money),
    }


__all__ = ["record_and_execute", "approve", "reject", "pending", "metrics", "EffectError"]
