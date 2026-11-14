"""Fleet sweep: when one case exposes a serial-offender bug, queue the same remedy for every other victim.

Each victim gets a deterministic proposal (refund its damages, plus a cancel when the suspect permits it), judged by the
same firewall against a synthetic REFUND_DUE / HIGH verdict, and is then *always* queued as PENDING_APPROVAL with an
extra `sweep_batch` WARN check: a fleet-wide payout needs a human signature. Victims with nothing to refund, or whose
actions the firewall blocks outright (already refunded, already queued), are left out and audited. Never commits.
"""
from __future__ import annotations

import json
import re
import sqlite3

from munim import config
from munim.actions.executor import _settle_sweep, approve, record_and_execute
from munim.db.conn import audit, now_iso, one, rows
from munim.firewall.policy import evaluate, make_check, plural, worst
from munim.models import (
    ActionType,
    ActionVerdict,
    Decision,
    ExecutionResult,
    FirewallResult,
    Proposal,
    ProposedAction,
    SuspectResult,
    SweepResult,
    SweepVictim,
    Verdict,
    rupees,
)

SWEEP_WARN = "Fleet-sweep actions always need human sign-off before any money moves."


def _next_sweep_id(conn: sqlite3.Connection) -> str:
    nums = [int(m.group(1)) for r in rows(conn, "SELECT id FROM sweeps") if (m := re.fullmatch(r"SWP-(\d+)", str(r["id"])))]
    return f"SWP-{max(nums, default=0) + 1:04d}"


def _victim_plan(conn: sqlite3.Connection, suspect_id: str, v: SuspectResult) -> tuple[Verdict, Proposal]:
    remedies = list(dict.fromkeys([ActionType.REFUND_DEBITS, *v.remedies]))
    targets = {k: str(val) for k, val in (v.targets or {}).items() if val}
    verdict = Verdict(
        outcome="REFUND_DUE",
        culprits=[suspect_id],
        headline=f"Fleet sweep: {v.title or suspect_id} also hit {v.merchant_id}.",
        smoking_gun=list(v.evidence or []),
        damages_ledger_ids=list(v.damages_ledger_ids),
        damages_paise=int(v.damages_paise),
        targets=targets,
        remedies=remedies,
        confidence="HIGH",
        sweepable_suspect=suspect_id,
    )
    actions = [ProposedAction(action_type=ActionType.REFUND_DEBITS, amount_paise=int(v.damages_paise),
                              ledger_entry_ids=list(v.damages_ledger_ids),
                              reason=f"Fleet sweep for {suspect_id}: refund debits the same fault caused.")]
    sub_id = targets.get("subscription_id", "")
    if (ActionType.CANCEL_SUBSCRIPTION in remedies and sub_id and "," not in sub_id
            and one(conn, "SELECT 1 FROM subscriptions WHERE id = ? AND merchant_id = ? AND status = 'ACTIVE'", (sub_id, v.merchant_id))):
        actions.append(ProposedAction(action_type=ActionType.CANCEL_SUBSCRIPTION, target_id=sub_id,
                                      reason=f"Fleet sweep for {suspect_id}: stop the faulty billing."))
    dev = targets.get("device_id", "")
    if ActionType.SCHEDULE_PICKUP in remedies and dev and "," not in dev:
        actions.append(ProposedAction(action_type=ActionType.SCHEDULE_PICKUP, target_id=dev,
                                      reason=f"Fleet sweep for {suspect_id}: collect the Soundbox the merchant returned."))
    return verdict, Proposal(actions=actions, rationale=f"Deterministic fleet-sweep remedy for {suspect_id}.")


def _queue(av: ActionVerdict) -> ActionVerdict:
    """Force sign-off: add the sweep_batch WARN and lift ALLOW / CORRECTED to NEEDS_HUMAN. BLOCK stays BLOCK."""
    if av.decision == Decision.BLOCK or av.final is None:
        return av
    checks = [*av.checks, make_check("sweep_batch", "WARN", SWEEP_WARN)]
    return av.model_copy(update={"checks": checks, "decision": worst([av.decision, Decision.NEEDS_HUMAN])})


def create_sweep(conn: sqlite3.Connection, *, source_case_id: str, suspect_id: str, victims: list[SuspectResult],
                 today: str = config.DEMO_TODAY) -> SweepResult:
    """Evaluate every victim, then queue the survivors under one new SWP-#### row. sweep_id is None when nobody qualifies."""
    if not one(conn, "SELECT 1 FROM cases WHERE id = ?", (source_case_id,)):
        raise LookupError(f"source case {source_case_id} not found")

    seen: set[str] = set()
    planned: list[tuple[SuspectResult, dict, Verdict, FirewallResult]] = []
    skipped: list[dict] = []
    for v in victims:
        m = v.merchant_id
        if not m or m in seen:
            continue
        seen.add(m)
        merchant = one(conn, "SELECT id, business_name, city FROM merchants WHERE id = ?", (m,))
        if not merchant:
            skipped.append({"merchant_id": m, "why": "unknown merchant"})
            continue
        if int(v.damages_paise) <= 0 or not v.damages_ledger_ids:
            skipped.append({"merchant_id": m, "why": "no damages"})
            continue
        verdict, proposal = _victim_plan(conn, suspect_id, v)
        fr = evaluate(conn, merchant_id=m, proposal=proposal, verdict=verdict, case_id=None, today=today)
        queued = [_queue(av) for av in fr.actions]
        refund_ok = any(av.decision != Decision.BLOCK and av.final is not None and av.final.action_type == ActionType.REFUND_DEBITS
                        for av in queued)
        if not refund_ok:
            why = next((c.detail for av in fr.actions for c in av.checks if c.status == "FAIL"), "blocked by the firewall")
            skipped.append({"merchant_id": m, "why": why})
            continue
        planned.append((v, merchant, verdict, FirewallResult(decision=worst(av.decision for av in queued), actions=queued)))

    if not planned:
        audit(conn, source_case_id, "system", "sweep_skipped",
              {"suspect_id": suspect_id, "candidates": len(victims), "skipped": skipped[:50]})
        return SweepResult(sweep_id=None, suspect_id=suspect_id, merchant_count=0, total_paise=0, victims=[], action_ids=[])

    sweep_id = _next_sweep_id(conn)
    conn.execute(
        "INSERT INTO sweeps(id, source_case_id, suspect_id, merchant_count, total_paise, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (sweep_id, source_case_id, suspect_id, 0, 0, "PENDING", now_iso()),
    )
    out_victims: list[SweepVictim] = []
    action_ids: list[int] = []
    total = 0
    for v, merchant, verdict, fr in planned:
        results = record_and_execute(conn, case_id=None, merchant_id=merchant["id"], firewall=fr, verdict=verdict,
                                     sweep_id=sweep_id, today=today)
        action_ids += [r.action_id for r in results if r.action_id is not None]
        refunds = [(av, r) for av, r in zip(fr.actions, results)
                   if r.status == "PENDING_APPROVAL" and r.action_type == ActionType.REFUND_DEBITS and av.final is not None]
        if not refunds:
            continue
        av = refunds[0][0]
        amount = int(av.final.amount_paise)
        total += amount
        out_victims.append(SweepVictim(merchant_id=merchant["id"], business_name=merchant["business_name"], city=merchant["city"],
                                       damages_paise=amount, ledger_entry_ids=list(av.final.ledger_entry_ids),
                                       targets=dict(verdict.targets)))

    conn.execute("UPDATE sweeps SET merchant_count = ?, total_paise = ? WHERE id = ?", (len(out_victims), total, sweep_id))
    audit(conn, source_case_id, "system", "sweep_created",
          {"sweep_id": sweep_id, "suspect_id": suspect_id, "merchant_count": len(out_victims), "total_paise": total,
           "summary": f"Queued {plural(len(out_victims), 'merchant')} for {rupees(total)} in refunds, pending sign-off.",
           "skipped": skipped[:50]})
    return SweepResult(sweep_id=sweep_id, suspect_id=suspect_id, merchant_count=len(out_victims), total_paise=total,
                       victims=out_victims, action_ids=action_ids)


def approve_sweep(conn: sqlite3.Connection, sweep_id: str, approver: str = "ops.lead",
                  merchant_ids: list[str] | None = None) -> list[ExecutionResult]:
    """Approve the sweep's pending actions through approve() (each one is re-checked, so a victim refunded in the
    meantime is BLOCKED, not paid twice). `merchant_ids` limits it to the merchants the reviewer ticked; the rest stay
    pending. The sweep becomes APPROVED once nothing is pending."""
    sweep = one(conn, "SELECT * FROM sweeps WHERE id = ?", (sweep_id,))
    if not sweep:
        raise LookupError(f"sweep {sweep_id} not found")
    pending_rows = rows(conn, "SELECT id, merchant_id FROM actions WHERE sweep_id = ? AND status = 'PENDING_APPROVAL' ORDER BY id",
                        (sweep_id,))
    picked = set(merchant_ids) if merchant_ids is not None else None
    ids = [r["id"] for r in pending_rows if picked is None or r["merchant_id"] in picked]
    results = [approve(conn, aid, approver) for aid in ids]
    _settle_sweep(conn, sweep_id)
    executed = [r for r in results if r.status == "EXECUTED"]
    paid = sum(r.amount_paise for r in executed if r.action_type == ActionType.REFUND_DEBITS)
    audit(conn, sweep["source_case_id"], "human", "sweep_approved",
          {"sweep_id": sweep_id, "approver": approver, "executed": len(executed), "refunded_paise": paid,
           "merchants": sorted(picked) if picked is not None else "all",
           "blocked": sum(r.status in ("BLOCKED", "DUPLICATE") for r in results), "failed": sum(r.status == "FAILED" for r in results)})
    return results


def _j(v: str | None, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def sweep_detail(conn: sqlite3.Connection, sweep_id: str) -> dict:
    """Everything a reviewer needs before signing a sweep, per merchant: the actions and their checks, the proof the
    same fault hit them, and the ledger rows the refund would reverse. Read-only."""
    sweep = one(conn, "SELECT * FROM sweeps WHERE id = ?", (sweep_id,))
    if not sweep:
        raise LookupError(f"sweep {sweep_id} not found")
    acts = rows(conn, """SELECT a.id, a.merchant_id, a.action_type, a.amount_paise, a.status, a.params_json, a.firewall_json,
                                a.verdict_json, a.decided_by, m.business_name, m.owner_name, m.city
                         FROM actions a JOIN merchants m ON m.id = a.merchant_id
                         WHERE a.sweep_id = ? ORDER BY a.id""", (sweep_id,))
    by_m: dict[str, dict] = {}
    for a in acts:
        params, fw, verdict = _j(a["params_json"], {}), _j(a["firewall_json"], {}), _j(a["verdict_json"], {})
        m = by_m.setdefault(a["merchant_id"], {
            "merchant_id": a["merchant_id"], "business_name": a["business_name"], "owner_name": a["owner_name"],
            "city": a["city"], "amount_paise": 0, "actions": [], "evidence": verdict.get("smoking_gun") or [],
            "targets": verdict.get("targets") or {}, "ledger_ids": [], "ledger": []})
        if a["action_type"] == ActionType.REFUND_DEBITS:
            m["amount_paise"] += int(a["amount_paise"] or 0)
            m["ledger_ids"] += [int(i) for i in params.get("ledger_entry_ids") or []]
        m["actions"].append({"action_id": a["id"], "action_type": a["action_type"], "amount_paise": a["amount_paise"],
                             "status": a["status"], "decided_by": a["decided_by"], "params": params,
                             "checks": fw.get("checks") or []})
    for m in by_m.values():
        ids = sorted(set(m.pop("ledger_ids")))
        if ids:
            marks = ",".join("?" * len(ids))
            m["ledger"] = rows(conn, f"""SELECT id, txn_ref, entry_type, amount_paise, status, value_date, narration
                                        FROM ledger_entries WHERE merchant_id = ? AND id IN ({marks}) ORDER BY value_date, id""",
                               (m["merchant_id"], *ids))
        sts = {x["status"] for x in m["actions"]}
        m["status"] = "PENDING_APPROVAL" if "PENDING_APPROVAL" in sts else ("EXECUTED" if "EXECUTED" in sts else sorted(sts)[0])
    source = one(conn, "SELECT c.id, c.complaint_text, m.business_name FROM cases c JOIN merchants m ON m.id = c.merchant_id "
                       "WHERE c.id = ?", (sweep["source_case_id"],))
    return {"sweep_id": sweep_id, "suspect_id": sweep["suspect_id"], "status": sweep["status"],
            "created_at": sweep["created_at"], "source_case": source, "merchant_count": len(by_m),
            "pending": sum(m["status"] == "PENDING_APPROVAL" for m in by_m.values()),
            "total_paise": sum(m["amount_paise"] for m in by_m.values()), "merchants": list(by_m.values())}


__all__ = ["create_sweep", "approve_sweep", "sweep_detail", "SWEEP_WARN"]
