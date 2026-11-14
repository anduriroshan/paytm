"""Gate 2, See: what the merchant app (and the merchant assistant) may read.

Everything here is built from explicit column lists, filtered to ONE shop, with internal fields replaced:
  risk-hold reason      -> "routine safety review"   (fraud signals never reach the merchant or the assistant)
  chargeback FRAUD      -> "disputed by the customer's bank"
  bank / txn refs       -> last 4 characters only
A case is shown as a plain timeline built from the actions table and an allowlist of graph steps; the team's
case file (suspects, firewall checks, LLM reasoning, fleet sweeps) is never returned.
"""
from __future__ import annotations

from typing import Any

from munim import config
from munim.db.conn import one, rows
from munim.forensics import facts
from munim.models import rupees

RISK_SAFE = "routine safety review"
HOLD_KIND = {"RISK": "Safety review", "KYC": "KYC update needed", "BANK_ACCOUNT": "Bank account check"}
CHARGEBACK_REASON = {"GOODS_NOT_RECEIVED": "customer says the goods did not arrive", "DUPLICATE": "customer says they paid twice",
                     "NOT_AS_DESCRIBED": "customer says the item was not as described"}
CHARGEBACK_SAFE = "disputed by the customer's bank"
ENTRY_LABEL = {"RENT_DEBIT": "Soundbox rent", "EDI_DEBIT": "Loan instalment", "SETTLEMENT_CREDIT": "Payout",
               "REFUND_CREDIT": "Refund", "CHARGEBACK_DEBIT": "Chargeback"}
PAYOUT_LABEL = {"SETTLED": "Paid", "RELEASED": "Paid", "HELD": "On hold"}
PICKUP_FAIL = {"SHOP_CLOSED": "shop was closed", "MERCHANT_UNAVAILABLE": "nobody was available at the shop"}
DOC_LABEL = {"DRUG_LICENSE": "Drug licence", "SHOP_LICENSE": "Shop licence", "PAN": "PAN", "AADHAAR": "Aadhaar", "GSTIN": "GSTIN"}
DEVICE_EVENT = {"INSTALLED": "Installed", "RETURN_REQUESTED": "Return requested", "PICKUP_SCHEDULED": "Pickup booked",
                "PICKUP_FAILED": "Pickup failed", "PICKED_UP": "Picked up", "RECEIVED_AT_WAREHOUSE": "Received at warehouse",
                "DEACTIVATED": "Deactivated", "SWAPPED_OUT": "Swapped out", "SWAPPED_IN": "Swapped in"}

CASE_LABEL = {"OPEN": "Received", "INVESTIGATING": "Checking", "RESOLVED": "Solved", "AWAITING_APPROVAL": "With our team",
              "ESCALATED": "With a specialist team", "BLOCKED": "Checked, no change", "FAILED": "With a teammate"}
CASE_TONE = {"INVESTIGATING": "run", "RESOLVED": "ok", "AWAITING_APPROVAL": "hum", "ESCALATED": "esc", "BLOCKED": "ok", "FAILED": "esc"}

# Graph steps the merchant may see while Munim works (node -> step index). Everything else stays internal.
PROGRESS = ("Reading your message", "Checking your account", "Fixing what I can", "Writing to you")
NODE_STEP = {"intake": 0, "hunch": 0, "trail": 1, "statements": 1, "lineup": 1, "improvise": 1, "verdict": 1,
             "propose": 2, "firewall": 2, "execute": 2, "respond": 3}


def mask(value: Any, keep: int = 4) -> str:
    s = str(value or "")
    return f"••{s[-keep:]}" if len(s) > keep else s


def _date(v: Any) -> str:
    return facts.fdate(v) if v else ""


# ───────────────────────────── snapshot ─────────────────────────────

def shop(conn, merchant_id: str) -> dict | None:
    return one(conn, "SELECT id, business_name, owner_name, city, language, category FROM merchants WHERE id=?", (merchant_id,))


def _payments_day(conn, mid: str) -> dict:
    """Payments on the demo 'today', or on the latest day with payments before it."""
    r = one(conn, """SELECT substr(paid_at,1,10) AS day FROM payments WHERE merchant_id=? AND status='SUCCESS'
                     AND substr(paid_at,1,10) <= ? ORDER BY paid_at DESC LIMIT 1""", (mid, config.DEMO_TODAY))
    if not r:
        return {"date": config.DEMO_TODAY, "label": "Today", "count": 0, "total": rupees(0), "total_paise": 0}
    day = r["day"]
    s = one(conn, """SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise),0) AS total FROM payments
                     WHERE merchant_id=? AND status='SUCCESS' AND substr(paid_at,1,10)=?""", (mid, day))
    return {"date": day, "label": "Today" if day == config.DEMO_TODAY else _date(day), "count": s["n"],
            "total": rupees(s["total"]), "total_paise": s["total"]}


def _payouts(conn, mid: str, limit: int = 6) -> list[dict]:
    out = []
    for s in rows(conn, """SELECT id, batch_date, gross_paise, deductions_paise, net_paise, status, utr FROM settlements
                           WHERE merchant_id=? AND batch_date <= ? ORDER BY batch_date DESC LIMIT ?""", (mid, config.DEMO_TODAY, limit)):
        out.append({"id": s["id"], "date": _date(s["batch_date"]), "amount": rupees(s["net_paise"]), "amount_paise": s["net_paise"],
                    "deductions": rupees(s["deductions_paise"]), "status": PAYOUT_LABEL.get(s["status"], s["status"].title()),
                    "bank_ref": mask(s["utr"]) if s["utr"] else ""})
    return out


def _holds(conn, mid: str) -> list[dict]:
    out = []
    for h in facts.holds(conn, mid):
        held = facts.covered_settlements(conn, h)
        kind = str(h["hold_type"]).upper()
        out.append({"id": h["id"], "kind": HOLD_KIND.get(kind, "Account check"),
                    "reason": RISK_SAFE if kind == "RISK" else h["reason_text"],
                    "since": _date(h["placed_at"]), "payouts_held": len(held),
                    "amount": rupees(sum(int(s["net_paise"]) for s in held)),
                    "fix": "Update the document with the KYC link" if kind == "KYC" else
                           ("Our team is reviewing it; no action needed from you" if kind == "RISK" else "Our team is checking it")})
    return out


def _kyc(conn, mid: str) -> list[dict]:
    return [{"doc": DOC_LABEL.get(d["doc_type"], d["doc_type"].title()), "status": d["status"].title(),
             "expires": _date(d["expires_at"])} for d in facts.kyc_docs(conn, mid)]


def _devices(conn, mid: str) -> list[dict]:
    devs = rows(conn, """SELECT DISTINCT d.id, d.serial_no, d.model, d.status, d.installed_at FROM devices d
                         WHERE d.merchant_id=? OR d.id IN (SELECT device_id FROM subscriptions WHERE merchant_id=?)
                         ORDER BY d.id""", (mid, mid))
    out = []
    for d in devs:
        evs = rows(conn, "SELECT event_type, at FROM device_events WHERE device_id=? ORDER BY at DESC, id DESC LIMIT 5", (d["id"],))
        tks = rows(conn, """SELECT id, status, scheduled_for, completed_at, attempts, failure_reason FROM pickup_tickets
                            WHERE device_id=? AND merchant_id=? ORDER BY requested_at DESC LIMIT 3""", (d["id"], mid))
        out.append({"id": d["id"], "serial": mask(d["serial_no"]), "model": d["model"], "status": d["status"].replace("_", " ").title(),
                    "events": [{"what": DEVICE_EVENT.get(e["event_type"], e["event_type"].title()), "date": _date(e["at"])} for e in evs],
                    "pickups": [{"id": t["id"], "status": t["status"].title(), "date": _date(t["scheduled_for"]),
                                 "done": _date(t["completed_at"]), "attempts": t["attempts"],
                                 "why_failed": PICKUP_FAIL.get(t["failure_reason"] or "", "") if t["failure_reason"] else ""}
                                for t in tks]})
    return out


def _plans(conn, mid: str) -> list[dict]:
    return [{"plan": r["name"], "fee": rupees(r["monthly_fee_paise"]), "status": r["status"].title(),
             "since": _date(r["started_at"]), "ended": _date(r["ended_at"])}
            for r in rows(conn, """SELECT p.name, p.monthly_fee_paise, s.status, s.started_at, s.ended_at FROM subscriptions s
                                   JOIN plans p ON p.id = s.plan_id WHERE s.merchant_id=? ORDER BY s.started_at""", (mid,))]


def _loans(conn, mid: str) -> list[dict]:
    return [{"lender": r["lender"], "daily_instalment": rupees(r["edi_amount_paise"]), "status": r["status"].title(),
             "since": _date(r["disbursed_at"])}
            for r in rows(conn, "SELECT lender, edi_amount_paise, status, disbursed_at FROM loans WHERE merchant_id=?", (mid,))]


def _chargebacks(conn, mid: str) -> list[dict]:
    return [{"id": r["id"], "amount": rupees(r["amount_paise"]),
             "reason": CHARGEBACK_SAFE if r["reason"] == "FRAUD" else CHARGEBACK_REASON.get(r["reason"], CHARGEBACK_SAFE),
             "status": r["status"].title(), "raised": _date(r["raised_at"]), "debited": _date(r["debited_at"])}
            for r in rows(conn, """SELECT id, amount_paise, reason, status, raised_at, debited_at FROM chargebacks
                                   WHERE merchant_id=? ORDER BY raised_at DESC LIMIT 5""", (mid,))]


def _passbook(conn, mid: str, limit: int = 12) -> list[dict]:
    return [{"date": _date(r["value_date"]), "what": ENTRY_LABEL.get(r["entry_type"], r["entry_type"].title()),
             "direction": r["direction"].title(), "amount": rupees(r["amount_paise"]), "status": r["status"].title(),
             "note": r["narration"], "ref": mask(r["txn_ref"], 6)}
            for r in rows(conn, """SELECT entry_type, direction, amount_paise, status, value_date, narration, txn_ref FROM ledger_entries
                                   WHERE merchant_id=? AND entry_type != 'SETTLEMENT_CREDIT' ORDER BY value_date DESC, id DESC LIMIT ?""",
                          (mid, limit))]


def snapshot(conn, merchant_id: str) -> dict[str, Any]:
    """Everything the merchant assistant may know about this shop. Nothing about other shops, nothing internal."""
    return {
        "today": config.DEMO_TODAY,
        "shop": shop(conn, merchant_id),
        "payments": _payments_day(conn, merchant_id),
        "payouts": _payouts(conn, merchant_id),
        "holds": _holds(conn, merchant_id),
        "kyc": _kyc(conn, merchant_id),
        "devices": _devices(conn, merchant_id),
        "plans": _plans(conn, merchant_id),
        "loans": _loans(conn, merchant_id),
        "chargebacks": _chargebacks(conn, merchant_id),
        "passbook": _passbook(conn, merchant_id),
        "issues": [case_card(conn, c) for c in own_cases(conn, merchant_id, limit=5)],
    }


# ───────────────────────────── cases, as the merchant sees them ─────────────────────────────

CASE_COLS = "id, merchant_id, channel, status, complaint_text, created_at, closed_at"


def own_cases(conn, merchant_id: str, limit: int = 20) -> list[dict]:
    return rows(conn, f"SELECT {CASE_COLS} FROM cases WHERE merchant_id=? ORDER BY CAST(SUBSTR(id, 6) AS INTEGER) DESC LIMIT ?",
                (merchant_id, limit))


def own_case(conn, merchant_id: str, case_id: str) -> dict | None:
    """Ownership check: another shop's case is simply 'not found'."""
    return one(conn, f"SELECT {CASE_COLS} FROM cases WHERE id=? AND merchant_id=?", (case_id, merchant_id))


def _action_step(a: dict) -> dict | None:
    t, st, amt = a["action_type"], a["status"], rupees(a["amount_paise"] or 0)
    done = st == "EXECUTED"
    when = _date(a["executed_at"] or a["created_at"])
    if st in ("BLOCKED", "FAILED", "DUPLICATE") or t == "NO_ACTION":
        return None
    label = {
        "SCHEDULE_PICKUP": ("Pickup booked", "Pickup: with our team"),
        "SEND_KYC_LINK": ("KYC link sent by SMS", "KYC link: with our team"),
        "REFUND_DEBITS": (f"Refund {amt} credited", f"Refund {amt}: with our team for approval"),
        "CANCEL_SUBSCRIPTION": ("Rent stopped", "Stopping rent: with our team"),
        "RELEASE_HOLD": ("Payout released", "Payout release: with our team"),
        "ESCALATE": ("Passed to a specialist team", "Passing to a specialist team"),
    }.get(t)
    if not label:
        return None
    if st == "REJECTED":
        return {"label": label[1].split(":")[0] + ": not approved", "date": when, "state": "done"}
    return {"label": label[0] if done else label[1], "date": when if done else "", "state": "done" if done else "now"}


def timeline(conn, case: dict) -> list[dict]:
    steps = [{"label": "Received", "date": _date(case["created_at"]), "state": "done"}]
    if case["status"] == "INVESTIGATING":
        steps.append({"label": "Checking your account", "date": "", "state": "now"})
        return steps
    steps.append({"label": "Checked your account", "date": _date(case["created_at"]), "state": "done"})
    acts = rows(conn, "SELECT action_type, status, amount_paise, created_at, executed_at FROM actions WHERE case_id=? ORDER BY id",
                (case["id"],))
    steps += [s for s in (_action_step(a) for a in acts) if s]
    if any(s["state"] == "now" for s in steps):
        steps.append({"label": "Next update here, when the team decides", "date": "", "state": "next"})
    elif case["status"] in ("RESOLVED", "BLOCKED"):
        steps.append({"label": "Solved" if case["status"] == "RESOLVED" else "Closed", "date": _date(case["closed_at"]), "state": "done"})
    return steps


def progress(case_id: str) -> list[dict]:
    """While Munim works: four plain steps from the allowlisted graph nodes."""
    from munim.graph import runner
    run = runner.RUNS.get(case_id)
    at = -1
    for e in (run.events if run else []):
        if str(e.type) == "node" and isinstance(e.data, dict) and e.data.get("node") in NODE_STEP:
            at = max(at, NODE_STEP[e.data["node"]])
    return [{"label": p, "state": "done" if i < at else ("now" if i == max(at, 0) else "next")} for i, p in enumerate(PROGRESS)]


def latest_reply(case_id: str) -> dict | None:
    """Munim's last message that actually went TO THE MERCHANT on this case: a reply or update a teammate sent
    (kind 'sent'), or one written in the merchant app's own chat. Drafts waiting for a teammate and team-only
    chat answers (kind 'chat') are never included."""
    from munim.graph import runner
    run = runner.load_run(case_id)
    msg = None
    for e in (run.events if run else []):
        d = e.data if isinstance(e.data, dict) else {}
        if str(e.type) != "message" or d.get("role") != "munim" or not d.get("final") or not d.get("text") or d.get("draft"):
            continue
        if d.get("kind") == "sent":
            kind = "update" if d.get("reply_kind") == "update" else "reply"
        elif d.get("kind") in (None, "update"):
            kind = d.get("kind") or "reply"
        else:
            continue
        msg = {"text": d["text"], "en": d.get("en") or "", "language": d.get("language") or "en", "kind": kind}
    return msg


def case_card(conn, case: dict, *, with_reply: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": case["id"], "status": case["status"], "label": CASE_LABEL.get(case["status"], "Received"),
        "tone": CASE_TONE.get(case["status"], "run"), "opened": _date(case["created_at"]),
        "text": case["complaint_text"][:160], "from_app": case["channel"] == "app",
        "done": case["status"] != "INVESTIGATING", "steps": timeline(conn, case),
    }
    if not out["done"]:
        out["progress"] = progress(case["id"])
    if with_reply and out["done"]:
        from munim.access.guard import guard_reply
        rep = latest_reply(case["id"])
        out["reply"] = guard_reply(conn, case["merchant_id"], rep, case_id=case["id"]) if rep else None
    return out


# ───────────────────────────── suggestions (data-driven chips) ─────────────────────────────

def suggestions(snap: dict) -> list[dict]:
    """Chips for the chat, picked from this shop's own data. `text` is what gets sent."""
    out: list[dict] = []
    issues = [i for i in snap.get("issues") or [] if i["status"] in ("INVESTIGATING", "AWAITING_APPROVAL", "ESCALATED")]
    if issues:
        out.append({"label": "Update on my issue?", "text": f"Any update on my issue {issues[0]['id']}?"})
    if snap.get("holds") or any(p["status"] == "On hold" for p in snap.get("payouts") or []):
        out.append({"label": "Where is my payout?", "text": "My payout has not come. Where is my money?"})
    debits = [p for p in snap.get("passbook") or [] if p["direction"] == "Debit" and p["what"] in ("Soundbox rent", "Loan instalment")]
    if debits:
        d = debits[0]
        out.append({"label": f"Why was {d['amount']} cut?", "text": f"Why was {d['amount']} {d['what'].lower()} cut on {d['date']}?"})
    cbs = [c for c in snap.get("chargebacks") or [] if c["status"] in ("Debited", "Open")]
    if cbs:
        out.append({"label": "Why is my payout short?", "text": "My payout is short this week. Why was money deducted?"})
    out.append({"label": "Today's payments", "text": "How much did I receive today?"})
    seen, uniq = set(), []
    for s in out:
        if s["label"] not in seen:
            seen.add(s["label"])
            uniq.append(s)
    return uniq[:4]
