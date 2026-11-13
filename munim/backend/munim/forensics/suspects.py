"""The lineup: a catalogue of known failure patterns ("suspects"), each with a deterministic test.

A test either MATCHes (summary = the smoking gun) or is ELIMINATED (summary = the alibi). Suspects outside the complaint's
scope, or with no data to test, are NOT_APPLICABLE but still reported so the lineup is always complete. Damages come only
from ledger rows: SUCCESS debits that the pattern says should never have happened and that have no REFUND_CREDIT yet.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Literal

from pydantic import BaseModel

from munim.forensics import facts as F
from munim.models import ActionType, EvidenceRef, Intent, IntakeResult, SuspectResult, Trail, rupees

A = ActionType


class SuspectSpec(BaseModel):
    id: str
    title: str
    kind: Literal["FAULT", "INNOCENT", "HOLD"]
    description: str
    intents: list[Intent]
    remedies: list[ActionType]
    sweepable: bool = False


CATALOGUE: dict[str, SuspectSpec] = {s.id: s for s in [
    SuspectSpec(
        id="ZOMBIE_BILLING", title="Billing continued after device return", kind="FAULT",
        description="The merchant returned the Soundbox and the courier picked it up, but the rental subscription was never "
                    "cancelled, so monthly rent kept being debited after the pickup.",
        intents=[Intent.RENT_DEDUCTION], remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION], sweepable=True),
    SuspectSpec(
        id="DUPLICATE_SUBSCRIPTION", title="Two subscriptions on one device", kind="FAULT",
        description="One Soundbox is billed by two active rental subscriptions (for example after a duplicate re-onboarding), "
                    "so the merchant pays the monthly rent twice.",
        intents=[Intent.RENT_DEDUCTION], remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION], sweepable=True),
    SuspectSpec(
        id="DUPLICATE_EDI", title="Loan instalment debited twice", kind="FAULT",
        description="The same daily loan EDI was debited more than once for one due date, typically when the gateway timed "
                    "out but the bank still debited and the retry debited again.",
        intents=[Intent.LOAN_DEDUCTION], remedies=[A.REFUND_DEBITS], sweepable=True),
    SuspectSpec(
        id="PICKUP_NOT_COMPLETED", title="Return requested but never collected", kind="INNOCENT",
        description="The merchant asked to return the Soundbox but the pickup never happened and the device is still taking "
                    "payments in the shop, so the rent being charged is legitimate.",
        intents=[Intent.RENT_DEDUCTION], remedies=[A.SCHEDULE_PICKUP]),
    SuspectSpec(
        id="ALREADY_REFUNDED", title="Already refunded", kind="INNOCENT",
        description="The debits the merchant is complaining about were already refunded, so nothing more is owed.",
        intents=[Intent.RENT_DEDUCTION, Intent.LOAN_DEDUCTION], remedies=[A.NO_ACTION]),
    SuspectSpec(
        id="RISK_HOLD", title="Settlement held by risk", kind="HOLD",
        description="A payout is held by the risk team (for example a card-velocity spike); only risk can release it after review.",
        intents=[Intent.SETTLEMENT_ISSUE], remedies=[A.ESCALATE]),
    SuspectSpec(
        id="KYC_HOLD", title="Settlement held for KYC", kind="HOLD",
        description="Payouts are held because a KYC document (such as a drug or shop licence) expired or is pending; "
                    "updating the document releases them.",
        intents=[Intent.SETTLEMENT_ISSUE], remedies=[A.SEND_KYC_LINK]),
]}

_AREAS = {"ZOMBIE_BILLING": {F.RENT}, "DUPLICATE_SUBSCRIPTION": {F.RENT}, "DUPLICATE_EDI": {F.LOAN},
          "PICKUP_NOT_COMPLETED": {F.RENT}, "ALREADY_REFUNDED": {F.RENT, F.LOAN}, "RISK_HOLD": {F.SETTLEMENT},
          "KYC_HOLD": {F.SETTLEMENT}}
_TOPIC = {F.RENT: "Soundbox rent", F.LOAN: "loan EDI", F.SETTLEMENT: "settlements"}
_ORDER = {"MATCH": 0, "ELIMINATED": 1, "NOT_APPLICABLE": 2}


def _result(sid: str, status: str, summary: str, evidence: list[EvidenceRef] | None = None, *,
            damages: list[dict] | None = None, targets: dict[str, str] | None = None, remedies: list[ActionType] | None = None,
            merchant_id: str | None = None) -> SuspectResult:
    spec = CATALOGUE[sid]
    damages = damages or []
    seen: set[tuple[str, str]] = set()
    ev = []
    for e in evidence or []:
        if (e.table, e.row_id) not in seen and e.row_id:
            seen.add((e.table, e.row_id))
            ev.append(e)
    return SuspectResult(
        suspect_id=sid, title=spec.title, kind=spec.kind, status=status, summary=summary, evidence=ev[:14],
        damages_ledger_ids=[int(d["id"]) for d in damages], damages_paise=sum(int(d["amount_paise"]) for d in damages),
        targets={k: v for k, v in (targets or {}).items() if v}, remedies=list(remedies or []), sweepable=spec.sweepable,
        merchant_id=merchant_id)


def _amounts(rows: list[dict]) -> str:
    """'2 rent debits of ₹499' / '3 debits (₹499, ₹299)'."""
    vals = sorted({r["amount_paise"] for r in rows})
    return rupees(vals[0]) if len(vals) == 1 else ", ".join(rupees(v) for v in vals)


# ───────────────────────────── FAULT tests ─────────────────────────────

def _zombie(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "ZOMBIE_BILLING"
    zs = F.zombie_analysis(conn, mid)
    if not zs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no Soundbox rental subscription, so there is no rent to test.")
    hits = [z for z in zs if z.damages]
    if hits:
        z = hits[0]
        dmg = [d for h in hits for d in h.damages]
        lc = z.lc
        awb = f" (AWB {lc.pickup_ref})" if lc.pickup_ref else ""
        still = "is still ACTIVE" if z.sub["status"] == "ACTIVE" else f"was only {z.sub['status'].lower()} later"
        extra = f" (plus {F.plural(len(hits) - 1, 'other subscription')})" if len(hits) > 1 else ""
        summary = (f"Soundbox {z.sub['device_id']} was picked up on {F.fdate(lc.pickup_at)}{awb}, but {z.sub['id']} {still} and "
                   f"took {F.plural(len(z.damages), 'rent debit')} of {_amounts(z.damages)} after that ({F.fdates(d['value_date'] for d in z.damages)})"
                   f"{extra}: {rupees(sum(d['amount_paise'] for d in dmg))} never refunded.")
        ev = [F.event_ref(lc.pickup_event)] if lc.pickup_event else []
        if lc.ticket:
            ev.append(F.ticket_ref(lc.ticket))
        if lc.received:
            ev.append(F.event_ref(lc.received))
        ev.append(F.sub_ref(z.sub))
        ev += [F.ledger_ref(d) for d in dmg]
        last = F.payments_via(conn, z.sub["device_id"], mid)
        if last.count:
            ev.append(F.payments_ref(last, f"Last payment through {z.sub['device_id']}: {F.fdate(last.last)} {F.fhm(last.last)}"))
        subs = [h.sub for h in hits]
        remedies = [A.REFUND_DEBITS] + ([A.CANCEL_SUBSCRIPTION] if any(s["status"] == "ACTIVE" for s in subs) else [])
        targets = {"subscription_id": ",".join(s["id"] for s in subs if s["status"] == "ACTIVE") or ",".join(s["id"] for s in subs),
                   "device_id": ",".join(dict.fromkeys(s["device_id"] for s in subs))}
        return _result(sid, "MATCH", summary, ev, damages=dmg, targets=targets, remedies=remedies)

    refunded = [z for z in zs if z.after]
    if refunded:
        z = refunded[0]
        ref = next(iter(F.refunds_of(conn, [d["id"] for d in z.after]).values()), None)
        when = f" on {F.fdate(ref['value_date'])}" if ref else ""
        summary = (f"Rent taken after the {F.fdate(z.lc.pickup_at)} pickup of {z.sub['device_id']} "
                   f"({F.fdates(d['value_date'] for d in z.after)}, {rupees(sum(d['amount_paise'] for d in z.after))}) "
                   f"was already refunded{when}, so nothing is owed.")
        ev = ([F.event_ref(z.lc.pickup_event)] if z.lc.pickup_event else []) + [F.ledger_ref(d) for d in z.after]
        ev += [F.ledger_ref(F.ledger_row(conn, d["refund_id"])) for d in z.after if d["refund_id"]]
        return _result(sid, "ELIMINATED", summary, ev)

    pending = [z for z in zs if z.lc and z.lc.request and not z.lc.pickup_at]
    if pending:
        z = pending[0]
        lc = z.lc
        pay = F.payments_via(conn, z.sub["device_id"], mid, after=lc.request["at"])
        fails = (f"{F.plural(len(lc.failed), 'pickup attempt')} failed ({F.failed_phrase(lc)})" if lc.failed
                 else "no courier has collected it")
        since = (f"{F.plural(pay.count, 'payment')} since, the last on {F.fdate(pay.last)}" if pay.count
                 else "no payments since")
        summary = (f"Never picked up: the return of {z.sub['device_id']} was requested on {F.fdate(lc.request['at'])} but "
                   f"{fails}, and the Soundbox has taken {since}, so the rent is not after a return.")
        ev = [F.event_ref(lc.request)] + [F.event_ref(e) for e in lc.failed]
        if lc.ticket:
            ev.append(F.ticket_ref(lc.ticket))
        if pay.count:
            ev.append(F.payments_ref(pay, f"{F.plural(pay.count, 'payment')} via {z.sub['device_id']} since the return request, "
                                          f"last {F.fdate(pay.last)} {F.fhm(pay.last)}"))
        return _result(sid, "ELIMINATED", summary, ev)

    returned = [z for z in zs if z.lc and z.lc.pickup_at]
    if returned:
        z = returned[0]
        end = (f"{z.sub['id']} was cancelled on {F.fdate(z.sub['ended_at'])}" if z.sub["status"] != "ACTIVE" and z.sub["ended_at"]
               else f"{z.sub['id']} has no rent debit after it")
        summary = (f"Soundbox {z.sub['device_id']} was picked up on {F.fdate(z.lc.pickup_at)} and {end}; "
                   f"no rent was debited after the pickup.")
        return _result(sid, "ELIMINATED", summary, ([F.event_ref(z.lc.pickup_event)] if z.lc.pickup_event else []) + [F.sub_ref(z.sub)])

    z = next((z for z in zs if z.sub["status"] == "ACTIVE"), zs[0])
    pay = F.payments_via(conn, z.sub["device_id"], mid, after=(ctx["today_d"] - timedelta(days=30)).isoformat())
    use = (f"it took {F.plural(pay.count, 'payment')} in the last 30 days, the last on {F.fdate(pay.last)}" if pay.count
           else "it is still assigned to the shop")
    summary = f"No return was ever requested for Soundbox {z.sub['device_id']}; {use}, so its rent is due."
    ev = [F.sub_ref(z.sub)]
    if pay.count:
        ev.append(F.payments_ref(pay, f"{F.plural(pay.count, 'payment')} via {z.sub['device_id']} in the last 30 days, last {F.fdate(pay.last)}"))
    return _result(sid, "ELIMINATED", summary, ev)


def _dup_sub(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "DUPLICATE_SUBSCRIPTION"
    subs = F.subscriptions(conn, mid)
    if not subs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no Soundbox rental subscription, so nothing can be billed twice.")
    groups = F.duplicate_subscriptions(conn, mid)
    hits = [g for g in groups if g.active and g.damages]
    if hits:
        g = hits[0]
        dmg = [d for h in hits for d in h.damages]
        later = [s for s in g.later if s["status"] == "ACTIVE"]
        dup_ids = [s["id"] for h in hits for s in h.later if s["status"] == "ACTIVE"]
        summary = (f"Soundbox {g.device_id} is billed by two active subscriptions, {g.original['id']} since "
                   f"{F.fdate(g.original['started_at'])} and {later[0]['id']} since {F.fdate(later[0]['started_at'])}, so "
                   f"{later[0]['id']}'s {F.plural(len(g.damages), 'rent debit')} of {_amounts(g.damages)} "
                   f"({F.fdates(d['value_date'] for d in g.damages)}) = {rupees(sum(d['amount_paise'] for d in dmg))} are double billing.")
        ev = [F.sub_ref(g.original)] + [F.sub_ref(s) for s in later] + [F.ledger_ref(d) for d in dmg]
        return _result(sid, "MATCH", summary, ev, damages=dmg,
                       targets={"subscription_id": ",".join(dup_ids), "device_id": ",".join(dict.fromkeys(h.device_id for h in hits))},
                       remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION])
    if groups:
        g = groups[0]
        refunded = [d for d in g.extra if d["refund_id"]]
        if g.active and refunded:
            summary = (f"{g.later[0]['id']} duplicates {g.original['id']} on {g.device_id}, but its "
                       f"{F.plural(len(refunded), 'extra debit')} ({rupees(sum(d['amount_paise'] for d in refunded))}) "
                       f"{F.was(len(refunded))} already refunded.")
        else:
            ended = next((s for s in [g.original, *g.later] if s["status"] != "ACTIVE"), None)
            how = f"{ended['id']} was {ended['status'].lower()} on {F.fdate(ended['ended_at'])}" if ended and ended["ended_at"] else "only one is active"
            summary = f"{g.device_id} has had {F.plural(1 + len(g.later), 'subscription')}, but {how}, so it is not billed twice now."
        return _result(sid, "ELIMINATED", summary, [F.sub_ref(s) for s in [g.original, *g.later]])
    active = [s for s in subs if s["status"] == "ACTIVE"]
    shown = active or subs
    if len(shown) == 1:
        s = shown[0]
        summary = f"Soundbox {s['device_id']} has a single subscription, {s['id']}, so nothing is billed twice."
    else:
        pairs = ", ".join(f"{s['id']} on {s['device_id']}" for s in shown[:4])
        summary = f"Each Soundbox has its own subscription ({pairs}), so nothing is billed twice."
    return _result(sid, "ELIMINATED", summary, [F.sub_ref(s) for s in shown[:4]])


def _dup_edi(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "DUPLICATE_EDI"
    les = F.edi_analysis(conn, mid)
    if not les:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no loan, so no EDI is collected.")
    hits = [le for le in les if le.damages]
    if hits:
        le = hits[0]
        dmg = [d for h in hits for d in h.damages]
        loan = le.loan
        days = [k for k, v in le.dup_days.items() if any(not d["refund_id"] for d in v[1:])]
        ev: list[EvidenceRef] = []
        if len(days) == 1:
            day = days[0]
            rows = le.dup_days[day]
            timeout = next((d for d in rows if d["attempt_status"] == "TIMEOUT"), None)
            if timeout and len(rows) == 2:
                other = rows[1] if rows[0] is timeout else rows[0]
                how = (f"attempt {timeout['attempt_no']} timed out at {F.ftime(timeout['responded_at'])} yet the bank debited "
                       f"{rupees(timeout['amount_paise'])} at {F.ftime(timeout['posted_at'])}, and the retry debited "
                       f"{rupees(other['amount_paise'])} again at {F.ftime(other['posted_at'])}")
            else:
                how = "the debits were posted at " + F.join_and([F.ftime(d["posted_at"]) for d in rows])
            summary = (f"Loan {loan['id']}'s {F.fdate(day)} EDI of {rupees(loan['edi_amount_paise'])} was debited "
                       f"{F.times_word(len(rows))}: {how}, so {rupees(sum(d['amount_paise'] for d in dmg))} is owed back.")
        else:
            summary = (f"Loan {loan['id']}'s EDI was debited more than once on {F.plural(len(days), 'due date')} "
                       f"({F.fdates(days)}): {F.plural(len(dmg), 'extra debit')} = {rupees(sum(d['amount_paise'] for d in dmg))} owed back.")
        for h in hits:
            for day, rows in h.dup_days.items():
                for a in F.attempts_for(conn, h.loan["id"], day):
                    ev.append(F.attempt_ref(a))
                ev += [F.ledger_ref(d) for d in rows]
        return _result(sid, "MATCH", summary, ev, damages=dmg,
                       targets={"loan_id": ",".join(h.loan["id"] for h in hits)}, remedies=[A.REFUND_DEBITS])
    le = les[0]
    refunded = [d for x in les for d in x.extra if d["refund_id"]]
    if refunded:
        summary = (f"Loan {le.loan['id']} had {F.plural(len(refunded), 'duplicate EDI debit')} "
                   f"({F.fdates(d['value_date'] for d in refunded)}), already refunded, so nothing is owed.")
        return _result(sid, "ELIMINATED", summary, [F.ledger_ref(d) for d in refunded])
    n = sum(len(x.by_due) for x in les)
    if not n:
        return _result(sid, "ELIMINATED", f"No EDI has been debited yet on loan {le.loan['id']}.")
    first = min(k for x in les for k in x.by_due)
    last = max(k for x in les for k in x.by_due)
    ids = ", ".join(x.loan["id"] for x in les)
    summary = (f"Loan {ids} has exactly one successful EDI debit per due date "
               f"({F.plural(n, 'day')}, {F.fspan(first, last)}), so nothing was debited twice.")
    latest = [rows[0] for k, rows in sorted(le.by_due.items())[-2:]]
    return _result(sid, "ELIMINATED", summary, [F.ledger_ref(d) for d in latest])


# ───────────────────────────── INNOCENT tests ─────────────────────────────

def _pickup(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "PICKUP_NOT_COMPLETED"
    subs = [s for s in F.subscriptions(conn, mid) if s["device_id"]]
    if not subs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no Soundbox rental, so there is no pickup to check.")
    lcs = [(s, F.lifecycle(conn, s["device_id"], mid, s["started_at"])) for s in subs]
    asked = [(s, lc) for s, lc in lcs if lc.request]
    if not asked:
        return _result(sid, "NOT_APPLICABLE", "No return was ever requested for the Soundbox, so there is no pickup to complete.")
    for s, lc in asked:
        if lc.pickup_at:
            continue
        pay = F.payments_via(conn, s["device_id"], mid, after=lc.request["at"])
        if not pay.count:
            continue
        fails = (f"{F.plural(len(lc.failed), 'pickup attempt')} failed ({F.failed_phrase(lc)})" if lc.failed
                 else "no pickup has happened")
        summary = (f"The return of {s['device_id']} was requested on {F.fdate(lc.request['at'])} but {fails}, and the Soundbox "
                   f"has taken {F.plural(pay.count, 'payment')} since ({F.fspan(pay.first, pay.last)}), so it is still in the "
                   f"shop and the rent is legitimate.")
        ev = [F.event_ref(lc.request)] + [F.event_ref(e) for e in lc.failed]
        if lc.ticket:
            ev.append(F.ticket_ref(lc.ticket))
        ev.append(F.payments_ref(pay, f"{F.plural(pay.count, 'payment')} via {s['device_id']} since the return request "
                                      f"({F.fspan(pay.first, pay.last)}), last {F.fdate(pay.last)} {F.fhm(pay.last)}"))
        ev.append(F.sub_ref(s))
        return _result(sid, "MATCH", summary, ev, targets={"device_id": s["device_id"], "subscription_id": s["id"]},
                       remedies=[A.SCHEDULE_PICKUP])
    s, lc = asked[-1]
    if lc.pickup_at:
        awb = f" (AWB {lc.pickup_ref})" if lc.pickup_ref else ""
        summary = f"The pickup of {s['device_id']} was completed on {F.fdate(lc.pickup_at)}{awb}, so the return is not stuck."
        ev = ([F.event_ref(lc.pickup_event)] if lc.pickup_event else []) + ([F.ticket_ref(lc.ticket)] if lc.ticket else [])
    else:
        summary = (f"The return of {s['device_id']} was requested on {F.fdate(lc.request['at'])} and the Soundbox has taken no "
                   f"payment since, so it is not still working in the shop.")
        ev = [F.event_ref(lc.request)]
    return _result(sid, "ELIMINATED", summary, ev)


def _already_refunded(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "ALREADY_REFUNDED"
    areas = ctx["areas"] & {F.RENT, F.LOAN}
    wrong = F.wrongful_debits(conn, mid, areas, include_refunded=True)
    candidates = set(wrong) | set(ctx["disputed_ledger"])
    debits = F.debit_rows(conn, mid, candidates)
    if not debits:
        return _result(sid, "NOT_APPLICABLE", "There is no disputed or wrongful debit, so there is nothing that could already have been refunded.")
    refunds = F.refunds_of(conn, [d["id"] for d in debits])
    done = [d for d in debits if d["id"] in refunds]
    if done:
        latest = max(refunds[d["id"]]["value_date"] for d in done)
        total = sum(refunds[d["id"]]["amount_paise"] for d in done)
        kinds = {F._ENTRY.get(d["entry_type"], "debit") for d in done}
        what = f"{next(iter(kinds))}s" if len(kinds) == 1 and len(done) > 1 else (next(iter(kinds)) if len(kinds) == 1 else "debits")
        open_ = [d for d in debits if d["id"] not in refunds and d["id"] in wrong]
        tail = (f"; {F.plural(len(open_), 'other wrongful debit')} ({rupees(sum(d['amount_paise'] for d in open_))}) "
                f"{F.was(len(open_))} not refunded yet") if open_ else ", so nothing more is owed"
        lead = f"The {F.num(len(done))} {what}" if len(done) > 1 else f"The {what}"
        summary = (f"{lead} at issue ({F.fdates(d['value_date'] for d in done)}) "
                   f"{F.was(len(done))} already refunded on {F.fdate(latest)} ({rupees(total)}){tail}.")
        ev = []
        for d in done:
            ev.append(F.ledger_ref(refunds[d["id"]]))
            ev.append(F.ledger_ref(d))
        return _result(sid, "MATCH", summary, ev, targets={"refunded_ledger_ids": ",".join(str(d["id"]) for d in done)},
                       remedies=[A.NO_ACTION])
    summary = (f"None of the {F.plural(len(debits), 'debit')} at issue ({F.fdates(d['value_date'] for d in debits)}, "
               f"{rupees(sum(d['amount_paise'] for d in debits))}) has been refunded yet.")
    if len(debits) == 1:
        summary = (f"The {F.fdate(debits[0]['value_date'])} debit of {rupees(debits[0]['amount_paise'])} at issue "
                   f"has not been refunded yet.")
    return _result(sid, "ELIMINATED", summary, [F.ledger_ref(d) for d in debits[-4:]])


# ───────────────────────────── HOLD tests ─────────────────────────────

def _hold_eliminated(conn, mid: str, sid: str, ctx: dict) -> SuspectResult:
    other = F.holds(conn, mid)
    label = "risk" if sid == "RISK_HOLD" else "KYC"
    stls = F.settlements(conn, mid, (ctx["today_d"] - timedelta(days=14)).isoformat())
    if other:
        h = other[0]
        return _result(sid, "ELIMINATED", f"There is no active {label} hold; the only active hold is {h['hold_type']} hold {h['id']}.",
                       [F.hold_ref(h)])
    if not stls:
        return _result(sid, "ELIMINATED", f"There is no active {label} hold on {mid}'s payouts.")
    held = [s for s in stls if s["status"] == "HELD"]
    last = stls[-1]
    if held:
        tail = f"{F.plural(len(held), 'settlement')} {F.was(len(held))} marked HELD without an active hold"
    elif last["status"] == "SETTLED":
        tail = f"the latest settlement, {last['id']}, was paid out on {F.fdate(last['settled_at'] or last['batch_date'])}" + \
               (f" (UTR {last['utr']})" if last.get("utr") else "")
    else:
        tail = f"the latest settlement, {last['id']}, is {last['status']}"
    return _result(sid, "ELIMINATED", f"There is no active {label} hold on {mid}'s payouts; {tail}.", [F.stl_ref(last)])


def _risk(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "RISK_HOLD"
    hs = F.holds(conn, mid, "RISK")
    if not hs:
        return _hold_eliminated(conn, mid, sid, ctx)
    h = hs[0]
    stls = F.covered_settlements(conn, h)
    reason = h["reason_text"].rstrip(".")
    held = F.stl_phrase(stls) if stls else "the merchant's payouts"
    verb = "has" if len(stls) <= 1 else "have"
    summary = (f"{held} {verb} been held since {F.fdate(h['placed_at'])} {F.fhm(h['placed_at'])} by risk hold {h['id']} "
               f"({h['reason_code']}: {F.lc_first(reason)}).")
    ev = [F.hold_ref(h)] + [F.stl_ref(s) for s in stls]
    return _result(sid, "MATCH", summary, ev,
                   targets={"hold_id": h["id"], "settlement_ids": ",".join(s["id"] for s in stls)}, remedies=[A.ESCALATE])


def _kyc(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "KYC_HOLD"
    hs = F.holds(conn, mid, "KYC")
    if not hs:
        return _hold_eliminated(conn, mid, sid, ctx)
    h = hs[0]
    stls = F.covered_settlements(conn, h)
    bad = [d for d in F.kyc_docs(conn, mid) if d["status"] in ("EXPIRED", "PENDING", "REJECTED")]
    why = F.join_and([F.doc_problem(d) for d in bad]) if bad else F.lc_first(h["reason_text"].rstrip("."))
    held = F.stl_phrase(stls) if stls else "The merchant's payouts"
    verb = "has" if len(stls) <= 1 else "have"
    summary = (f"{held} {verb} been held since {F.fdate(h['placed_at'])} by KYC hold {h['id']} because {why}; "
               f"updating the document releases them.")
    ev = [F.hold_ref(h)] + [F.kyc_ref(d) for d in bad] + [F.stl_ref(s) for s in stls]
    return _result(sid, "MATCH", summary, ev,
                   targets={"hold_id": h["id"], "settlement_ids": ",".join(s["id"] for s in stls)}, remedies=[A.SEND_KYC_LINK])


_TESTS: dict[str, Callable] = {"ZOMBIE_BILLING": _zombie, "DUPLICATE_SUBSCRIPTION": _dup_sub, "DUPLICATE_EDI": _dup_edi,
                               "PICKUP_NOT_COMPLETED": _pickup, "ALREADY_REFUNDED": _already_refunded, "RISK_HOLD": _risk,
                               "KYC_HOLD": _kyc}


# ───────────────────────────── public API ─────────────────────────────

def _today(today: str | None) -> date:
    from munim import config
    return F.as_date(today) or F.as_date(config.DEMO_TODAY) or date.today()


def interrogate(conn, merchant_id: str, intake: IntakeResult, trail: Trail, today: str) -> list[SuspectResult]:
    """Every suspect in the catalogue, MATCHes first (then ELIMINATED, then NOT_APPLICABLE; catalogue order within)."""
    sc = F.scope(conn, merchant_id, intake)
    ctx = {"today_d": _today(today), "areas": sc.areas, "disputed_ledger": F.disputed_ledger_ids(trail), "intake": intake}
    topic = F.join_and([_TOPIC[a] for a in F.AREAS if a in sc.areas]) or "nothing testable"
    out = []
    for sid in CATALOGUE:
        if not (_AREAS[sid] & sc.areas):
            areas = " or ".join(_TOPIC[a] for a in F.AREAS if a in _AREAS[sid])
            out.append(_result(sid, "NOT_APPLICABLE", f"Not tested: the complaint is about {topic}, not {areas}."))
            continue
        try:
            out.append(_TESTS[sid](conn, merchant_id, ctx))
        except Exception as e:   # one broken test must not hide the rest of the lineup
            out.append(_result(sid, "NOT_APPLICABLE", f"The {CATALOGUE[sid].title.lower()} test could not run ({type(e).__name__})."))
    return sorted(out, key=lambda s: _ORDER[s.status])


_CANDIDATES = {
    "ZOMBIE_BILLING": """SELECT DISTINCT s.merchant_id FROM subscriptions s
                         WHERE s.device_id IN (SELECT device_id FROM device_events WHERE event_type = 'PICKED_UP'
                                               UNION SELECT device_id FROM pickup_tickets WHERE status = 'COMPLETED')""",
    "DUPLICATE_SUBSCRIPTION": """SELECT DISTINCT merchant_id FROM subscriptions WHERE status = 'ACTIVE' AND device_id IS NOT NULL
                                 GROUP BY merchant_id, device_id HAVING COUNT(*) > 1""",
    "DUPLICATE_EDI": """SELECT DISTINCT l.merchant_id FROM ledger_entries l LEFT JOIN edi_attempts a ON a.ledger_entry_id = l.id
                        WHERE l.entry_type = 'EDI_DEBIT' AND l.status = 'SUCCESS' AND l.direction = 'DEBIT'
                        GROUP BY l.merchant_id, l.source_id, COALESCE(a.due_date, l.value_date) HAVING COUNT(*) > 1""",
}


def find_victims(conn, suspect_id: str, exclude_merchant_id: str | None, today: str) -> list[SuspectResult]:
    """Run one sweepable suspect's test across the fleet. Only MATCHes with damages > 0, merchant_id set, sorted by id."""
    spec = CATALOGUE.get(suspect_id)
    if not spec or not spec.sweepable or suspect_id not in _CANDIDATES:
        return []
    ctx = {"today_d": _today(today), "areas": set(F.AREAS), "disputed_ledger": [], "intake": None}
    out = []
    mids = sorted({r[0] for r in conn.execute(_CANDIDATES[suspect_id]).fetchall()} - {exclude_merchant_id})
    for mid in mids:
        try:
            r = _TESTS[suspect_id](conn, mid, ctx)
        except Exception:
            continue
        if r.status == "MATCH" and r.damages_paise > 0:
            out.append(r.model_copy(update={"merchant_id": mid}))
    return out
