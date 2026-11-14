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
from munim.forensics import travel as T
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
        id="RETURN_STUCK", title="Return stuck on our side", kind="FAULT",
        description="The merchant asked to return the Soundbox and stopped using it, but no courier collected it within the "
                    "pickup window (no failed attempt on the shop's side), and the monthly rent kept being debited.",
        intents=[Intent.RENT_DEDUCTION], remedies=[A.REFUND_DEBITS, A.CANCEL_SUBSCRIPTION, A.SCHEDULE_PICKUP], sweepable=True),
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
        intents=[Intent.RENT_DEDUCTION, Intent.LOAN_DEDUCTION, Intent.TRAVEL_REFUND], remedies=[A.NO_ACTION]),
    SuspectSpec(
        id="RISK_HOLD", title="Settlement held by risk", kind="HOLD",
        description="A payout is held by the risk team (for example a card-velocity spike); only risk can release it after review.",
        intents=[Intent.SETTLEMENT_ISSUE], remedies=[A.ESCALATE]),
    SuspectSpec(
        id="KYC_HOLD", title="Settlement held for KYC", kind="HOLD",
        description="Payouts are held because a KYC document (such as a drug or shop licence) expired or is pending; "
                    "updating the document releases them.",
        intents=[Intent.SETTLEMENT_ISSUE], remedies=[A.SEND_KYC_LINK]),
    SuspectSpec(
        id="BUS_TRIP_FAILED", title="Bus trip failed, fare not refunded", kind="FAULT",
        description="The bus turned back or broke down and never reached the destination (GPS and the driver's report), but "
                    "the operator never marked the trip cancelled, so no refund was triggered for passengers who boarded.",
        intents=[Intent.TRAVEL_REFUND], remedies=[A.REFUND_DEBITS, A.ESCALATE], sweepable=True),
    SuspectSpec(
        id="AIRLINE_REFUND_NOT_PASSED", title="Airline refund received, not passed on", kind="FAULT",
        description="The airline cancelled the flight and paid the fare back to us, but the payment came with only a PNR and "
                    "a name and was never matched to the booking, so it never reached the passenger.",
        intents=[Intent.TRAVEL_REFUND], remedies=[A.REFUND_DEBITS], sweepable=True),
    SuspectSpec(
        id="TRAVEL_NO_SHOW", title="Passenger did not board", kind="INNOCENT",
        description="The bus left without the passenger: there is no boarding scan for the booking, so under the no-show rule "
                    "the fare is not refunded.",
        intents=[Intent.TRAVEL_REFUND], remedies=[A.NO_ACTION]),
    SuspectSpec(
        id="AIRLINE_NOT_PAID", title="Airline has not paid back yet", kind="HOLD",
        description="The airline cancelled the flight but has not yet sent the fare back to us; the travel team has to chase "
                    "the airline.",
        intents=[Intent.TRAVEL_REFUND], remedies=[A.ESCALATE]),
]}

_AREAS = {"ZOMBIE_BILLING": {F.RENT}, "RETURN_STUCK": {F.RENT}, "DUPLICATE_SUBSCRIPTION": {F.RENT}, "DUPLICATE_EDI": {F.LOAN},
          "PICKUP_NOT_COMPLETED": {F.RENT}, "ALREADY_REFUNDED": {F.RENT, F.LOAN, F.TRAVEL}, "RISK_HOLD": {F.SETTLEMENT},
          "KYC_HOLD": {F.SETTLEMENT}, "BUS_TRIP_FAILED": {F.TRAVEL}, "AIRLINE_REFUND_NOT_PASSED": {F.TRAVEL},
          "TRAVEL_NO_SHOW": {F.TRAVEL}, "AIRLINE_NOT_PAID": {F.TRAVEL}}
_TOPIC = {F.RENT: "Soundbox rent", F.LOAN: "loan EDI", F.SETTLEMENT: "settlements", F.TRAVEL: "ticket refunds"}
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
                   f"{fails}, and the Soundbox has taken {since}, so no rent was taken after a pickup.")
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


def _not_collected(st: F.StuckReturn) -> str:
    """Why the courier has not collected it: 'the pickup booked for 25 Aug 2026 never happened' / courier-side failures."""
    lc = st.lc
    if lc.failed:
        return f"{F.plural(len(lc.failed), 'pickup attempt')} failed ({F.failed_phrase(lc)})"
    t = lc.ticket
    if t and t["scheduled_for"]:
        return f"the pickup booked for {F.fdate(t['scheduled_for'])} never happened"
    return "no courier was sent"


def _stuck(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "RETURN_STUCK"
    from munim import config
    subs = [s for s in F.subscriptions(conn, mid) if s["device_id"]]
    if not subs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no Soundbox rental, so there is no return to check.")
    window = config.RETURN_PICKUP_DAYS
    sts = F.stuck_returns(conn, mid, ctx["today_d"])
    hits = [st for st in sts if st.stuck and st.damages]
    if hits:
        st = hits[0]
        lc, dev = st.lc, st.sub["device_id"]
        dmg = [d for h in hits for d in h.damages]
        days = (ctx["today_d"] - F.as_date(lc.request["at"])).days
        extra = f" (plus {F.plural(len(hits) - 1, 'other device')})" if len(hits) > 1 else ""
        summary = (f"The return of {dev} was requested on {F.fdate(lc.request['at'])} and the Soundbox has taken no payment "
                   f"since, but {_not_collected(st)}: still not collected after {days} days. {st.sub['id']} took "
                   f"{F.plural(len(st.damages), 'rent debit')} of {_amounts(st.damages)} after the {window}-day pickup window "
                   f"({F.fdates(d['value_date'] for d in st.damages)}){extra}: "
                   f"{rupees(sum(d['amount_paise'] for d in dmg))} never refunded.")
        ev = [F.event_ref(lc.request)]
        ev += [F.event_ref(e) for e in lc.events if e["event_type"] == "PICKUP_SCHEDULED" and e["at"] >= lc.request["at"]][:1]
        ev += [F.event_ref(e) for e in lc.failed]
        if lc.ticket:
            ev.append(F.ticket_ref(lc.ticket))
        last = F.payments_via(conn, dev, mid, before=lc.request["at"])
        if last.count:
            ev.append(F.payments_ref(last, f"Last payment through {dev}: {F.fdate(last.last)} {F.fhm(last.last)}, "
                                           f"before the return request"))
        ev.append(F.sub_ref(st.sub))
        ev += [F.ledger_ref(d) for d in dmg]
        subs_hit = [h.sub for h in hits]
        remedies = [A.REFUND_DEBITS] + ([A.CANCEL_SUBSCRIPTION] if any(x["status"] == "ACTIVE" for x in subs_hit) else [])
        devices = list(dict.fromkeys(x["device_id"] for x in subs_hit))
        if len(devices) == 1:
            remedies.append(A.SCHEDULE_PICKUP)
        targets = {"subscription_id": ",".join(x["id"] for x in subs_hit if x["status"] == "ACTIVE")
                   or ",".join(x["id"] for x in subs_hit),
                   "device_id": ",".join(devices)}
        return _result(sid, "MATCH", summary, ev, damages=dmg, targets=targets, remedies=remedies)

    if not sts:
        lcs = [(x, F.lifecycle(conn, x["device_id"], mid, x["started_at"])) for x in subs]
        picked = [(x, lc) for x, lc in lcs if lc.pickup_at]
        if picked:
            x, lc = picked[-1]
            awb = f" (AWB {lc.pickup_ref})" if lc.pickup_ref else ""
            summary = f"Soundbox {x['device_id']} was collected on {F.fdate(lc.pickup_at)}{awb}, so its return is not stuck."
            ev = ([F.event_ref(lc.pickup_event)] if lc.pickup_event else []) + ([F.ticket_ref(lc.ticket)] if lc.ticket else [])
            return _result(sid, "ELIMINATED", summary, ev)
        return _result(sid, "NOT_APPLICABLE", "No return was ever requested for the Soundbox, so no return can be stuck.")

    st = sts[0]
    lc, dev = st.lc, st.sub["device_id"]
    req = F.fdate(lc.request["at"])
    ev = [F.event_ref(lc.request)] + [F.event_ref(e) for e in lc.failed] + ([F.ticket_ref(lc.ticket)] if lc.ticket else [])
    if st.pay.count:
        summary = (f"The return of {dev} was requested on {req}, but the Soundbox has taken {F.plural(st.pay.count, 'payment')} "
                   f"since (last on {F.fdate(st.pay.last)}), so it is still in use in the shop.")
        ev.append(F.payments_ref(st.pay, f"{F.plural(st.pay.count, 'payment')} via {dev} since the return request, "
                                         f"last {F.fdate(st.pay.last)} {F.fhm(st.pay.last)}"))
    elif st.merchant_failed:
        summary = (f"The courier came for {dev} after the {req} return request but could not collect it on the shop's side "
                   f"({F.failed_phrase(lc)}), so the delay is not ours.")
    elif not st.overdue:
        summary = (f"The return of {dev} was requested on {req}; the {window}-day pickup window runs until "
                   f"{F.fdate(st.due_by)}, so the pickup is not late yet.")
    elif st.after:
        summary = (f"Rent taken after the pickup window for {dev} ({F.fdates(d['value_date'] for d in st.after)}, "
                   f"{rupees(sum(d['amount_paise'] for d in st.after))}) was already refunded, so nothing is owed.")
        ev += [F.ledger_ref(d) for d in st.after]
    else:
        summary = (f"The return of {dev} requested on {req} is not collected yet, but no rent was debited after the "
                   f"{window}-day pickup window ({F.fdate(st.due_by)}).")
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
    areas = ctx["areas"] & {F.RENT, F.LOAN, F.TRAVEL}
    wrong = F.wrongful_debits(conn, mid, areas, include_refunded=True, today=ctx["today_d"])
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


# ───────────────────────────── travel (passenger accounts) ─────────────────────────────

def _bus_evidence(c: T.BusCheck) -> list[EvidenceRef]:
    b = c.booking
    ev = [T.booking_ref(b), T.trip_ref(b)]
    ev += [T.event_ref(e, b) for e in (c.departed, c.stopped, c.turned, c.report, c.back, c.arrived) if e]
    return ev


def _bus_failed(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "BUS_TRIP_FAILED"
    cs = T.bus_checks(conn, mid)
    if not cs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no bus booking, so there is no bus trip to check.")
    hits = [c for c in cs if c.owed]
    if hits:
        c = hits[0]
        b = c.booking
        dmg = [h.fare for h in hits]
        report = f"; the driver reported at {F.fhm(c.report['at'])}: “{c.report['note'].rstrip('.')}”" if c.report else ""
        status = ("the operator never marked it cancelled (status still " + b["trip_status"] + ")"
                  if b["trip_status"] != "CANCELLED" else "the operator marked it cancelled")
        extra = f" (plus {F.plural(len(hits) - 1, 'other ticket')})" if len(hits) > 1 else ""
        summary = (f"Bus {b['service_no']} ({T.route(b)}, {F.fday(b['departs_at'])}) turned back at {T.km(c.km)}: GPS shows "
                   f"it back at {b['origin']} at {F.fhm(c.back['at'])} and it never arrived{report}. Yet {status}. "
                   f"The passenger boarded (berth {b['seat']}, scanned {F.fhm(b['boarded_at'])}), and the "
                   f"{rupees(sum(int(d['amount_paise']) for d in dmg))} fare{extra} was never refunded.")
        ev = _bus_evidence(c) + [F.ledger_ref(d) for d in dmg]
        ev += [T.call_ref(x) for x in T.calls(conn, mid, b["id"])]
        targets = {"booking_id": ",".join(h.booking["id"] for h in hits),
                   "trip_id": ",".join(dict.fromkeys(h.booking["trip_id"] for h in hits)),
                   "operator": b["operator_name"]}
        return _result(sid, "MATCH", summary, ev, damages=dmg, targets=targets, remedies=[A.REFUND_DEBITS, A.ESCALATE])

    done = [c for c in cs if c.failed and c.refunded]
    if done:
        c = done[0]
        b = c.booking
        summary = (f"Bus {b['service_no']} on {F.fday(b['departs_at'])} turned back, but the {rupees(c.fare['amount_paise'])} "
                   f"fare was already refunded on {F.fdate(c.fare['refund_date'])}, so nothing is owed.")
        return _result(sid, "ELIMINATED", summary, [T.booking_ref(b), F.ledger_ref(c.fare)])
    skipped = [c for c in cs if c.no_show]
    if skipped:
        b = skipped[0].booking
        summary = (f"Bus {b['service_no']} turned back on {F.fday(b['departs_at'])}, but there is no boarding scan for "
                   f"{b['id']}: the passenger was not on it.")
        return _result(sid, "ELIMINATED", summary, _bus_evidence(skipped[0]))
    c = cs[-1]
    b = c.booking
    if c.arrived:
        summary = (f"Bus {b['service_no']} ({T.route(b)}) reached {b['destination']} at {F.fhm(c.arrived['at'])} on "
                   f"{F.fday(c.arrived['at'])} (GPS), so the trip happened.")
    elif not c.confirmed:
        summary = f"Booking {b['id']} was cancelled on {F.fdate(b['cancelled_at'])}" + \
                  (f" and refunded on {F.fdate(c.fare['refund_date'])}." if c.refunded else ".")
    else:
        summary = (f"No GPS or driver record shows bus {b['service_no']} turning back; the operator status is "
                   f"{b['trip_status']}.")
    return _result(sid, "ELIMINATED", summary, _bus_evidence(c))


def _no_show(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "TRAVEL_NO_SHOW"
    cs = [c for c in T.bus_checks(conn, mid) if c.departed]
    if not cs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no bus booking on a bus that left, so there is no boarding to check.")
    hits = [c for c in cs if c.confirmed and not c.boarded and not c.refunded]
    if hits:
        c = hits[0]
        b = c.booking
        summary = (f"Bus {b['service_no']} left {b['origin']} at {F.fhm(c.departed['at'])} on {F.fday(c.departed['at'])} "
                   f"with no boarding scan for {b['id']} (berth {b['seat']}): the passenger did not board, so the "
                   f"{rupees(b['fare_paise'])} fare is not refunded under the no-show rule.")
        return _result(sid, "MATCH", summary, _bus_evidence(c) + ([F.ledger_ref(c.fare)] if c.fare else []),
                       targets={"booking_id": b["id"]}, remedies=[A.NO_ACTION])
    c = next((c for c in cs if c.boarded), cs[-1])
    b = c.booking
    if c.boarded:
        summary = f"The passenger boarded: {b['id']} (berth {b['seat']}) was scanned at {F.fhm(b['boarded_at'])} on {F.fday(b['boarded_at'])}."
    else:
        summary = f"Booking {b['id']} was cancelled or refunded before the trip, so this is not a no-show."
    return _result(sid, "ELIMINATED", summary, [T.booking_ref(b)])


def _flight_evidence(conn, mid: str, c: T.FlightCheck) -> list[EvidenceRef]:
    b = c.booking
    ev = [T.booking_ref(b)] + ([T.event_ref(c.cancel, b)] if c.cancel else [])
    if c.paid:
        ev.append(T.airline_refund_ref(c.paid, b["operator_name"]))
    return ev


def _airline_not_passed(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "AIRLINE_REFUND_NOT_PASSED"
    cs = T.flight_checks(conn, mid, ctx["today_d"])
    if not cs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no flight booking, so there is no airline refund to trace.")
    hits = [c for c in cs if c.stuck]
    if hits:
        c = hits[0]
        b, paid = c.booking, c.paid
        dmg = [h.fare for h in hits]
        late = [x for x in T.calls(conn, mid, b["id"]) if x["at"] > paid["received_at"]]
        helpline = (f" On {F.fdate(late[-1]['at'])}, {(F.as_date(late[-1]['at']) - F.as_date(paid['received_at'])).days} days "
                    f"after the money arrived, the helpline still told the passenger we were waiting for the airline.") if late else ""
        extra = f" (plus {F.plural(len(hits) - 1, 'other booking')})" if len(hits) > 1 else ""
        summary = (f"{b['operator_name']} cancelled {b['service_no']} ({T.route(b)}, {F.fdate(b['departs_at'])}) and paid "
                   f"{rupees(paid['amount_paise'])} back to us on {F.fdate(paid['received_at'])} ({paid['id']}, PNR {paid['pnr']}), "
                   f"but it was never matched to booking {b['id']}{extra}: no refund in {c.days} days, past the "
                   f"{T.PASS_ON_DAYS}-day pass-on limit.{helpline}")
        ev = _flight_evidence(conn, mid, c) + [F.ledger_ref(d) for d in dmg]
        ev += [T.call_ref(x) for x in T.calls(conn, mid, b["id"])]
        targets = {"booking_id": ",".join(h.booking["id"] for h in hits),
                   "airline_refund_id": ",".join(h.paid["id"] for h in hits), "pnr": ",".join(h.booking["pnr"] for h in hits)}
        return _result(sid, "MATCH", summary, ev, damages=dmg, targets=targets, remedies=[A.REFUND_DEBITS])

    c = cs[-1]
    b = c.booking
    if c.refunded:
        summary = f"The {rupees(c.fare['amount_paise'])} fare for {b['id']} was refunded on {F.fdate(c.fare['refund_date'])}."
    elif c.in_window:
        due = F.as_date(c.paid["received_at"]) + timedelta(days=T.PASS_ON_DAYS)
        summary = (f"{b['operator_name']} paid {rupees(c.paid['amount_paise'])} back on {F.fdate(c.paid['received_at'])}, "
                   f"{F.plural(c.days or 0, 'day')} ago: still inside the {T.PASS_ON_DAYS}-day window (due by {F.fdate(due)}).")
    elif c.not_paid:
        summary = f"{b['operator_name']} has not paid anything back for PNR {b['pnr']} yet, so there is nothing of ours to pass on."
    else:
        summary = f"Flight {b['service_no']} on {F.fdate(b['departs_at'])} was not cancelled, so no airline refund is due."
    return _result(sid, "ELIMINATED", summary, _flight_evidence(conn, mid, c))


def _airline_not_paid(conn, mid: str, ctx: dict) -> SuspectResult:
    sid = "AIRLINE_NOT_PAID"
    cs = [c for c in T.flight_checks(conn, mid, ctx["today_d"]) if c.cancelled]
    if not cs:
        return _result(sid, "NOT_APPLICABLE", f"{mid} has no cancelled flight, so no airline refund is awaited.")
    hits = [c for c in cs if c.not_paid]
    if hits:
        c = hits[0]
        b = c.booking
        since = F.as_date(b["cancelled_at"] or b["trip_status_at"])
        days = (ctx["today_d"] - since).days if since else 0
        summary = (f"{b['operator_name']} cancelled {b['service_no']} ({T.route(b)}, {F.fdate(b['departs_at'])}) but has not "
                   f"paid the {rupees(b['fare_paise'])} fare back for PNR {b['pnr']} in {F.plural(days, 'day')}; "
                   f"the travel team has to chase the airline.")
        return _result(sid, "MATCH", summary, _flight_evidence(conn, mid, c),
                       targets={"booking_id": b["id"], "pnr": b["pnr"], "operator": b["operator_name"]}, remedies=[A.ESCALATE])
    c = next((c for c in cs if c.paid), cs[-1])
    b = c.booking
    if c.paid:
        summary = (f"{b['operator_name']} already paid {rupees(c.paid['amount_paise'])} back on {F.fdate(c.paid['received_at'])} "
                   f"({c.paid['id']}), so the money is not waiting on the airline.")
    else:
        summary = f"The fare for {b['id']} was already refunded, so nothing is waiting on the airline."
    return _result(sid, "ELIMINATED", summary, _flight_evidence(conn, mid, c))


_TESTS: dict[str, Callable] = {"ZOMBIE_BILLING": _zombie, "RETURN_STUCK": _stuck, "DUPLICATE_SUBSCRIPTION": _dup_sub, "DUPLICATE_EDI": _dup_edi,
                               "PICKUP_NOT_COMPLETED": _pickup, "ALREADY_REFUNDED": _already_refunded, "RISK_HOLD": _risk,
                               "KYC_HOLD": _kyc, "BUS_TRIP_FAILED": _bus_failed, "AIRLINE_REFUND_NOT_PASSED": _airline_not_passed,
                               "TRAVEL_NO_SHOW": _no_show, "AIRLINE_NOT_PAID": _airline_not_paid}


# ───────────────────────────── public API ─────────────────────────────

def _today(today: str | None) -> date:
    from munim import config
    return F.as_date(today) or F.as_date(config.DEMO_TODAY) or date.today()


def lineup(conn, merchant_id: str, intake: IntakeResult | None = None) -> list[str]:
    """The suspects that fit this account: a passenger gets the ticket-refund suspects, a shop the shop ones
    (plus the ticket-refund ones if the complaint is about a ticket). ALREADY_REFUNDED fits both."""
    if F.is_passenger(conn, merchant_id):
        return [sid for sid in CATALOGUE if F.TRAVEL in _AREAS[sid]]
    travel = intake is not None and intake.intent == Intent.TRAVEL_REFUND
    return [sid for sid in CATALOGUE if (_AREAS[sid] - {F.TRAVEL}) or travel]


def interrogate(conn, merchant_id: str, intake: IntakeResult, trail: Trail, today: str) -> list[SuspectResult]:
    """Every suspect that fits the account, MATCHes first (then ELIMINATED, then NOT_APPLICABLE; catalogue order within)."""
    sc = F.scope(conn, merchant_id, intake)
    ctx = {"today_d": _today(today), "areas": sc.areas, "disputed_ledger": F.disputed_ledger_ids(trail), "intake": intake}
    topic = F.join_and([_TOPIC[a] for a in F.ALL_AREAS if a in sc.areas]) or "nothing testable"
    out = []
    for sid in lineup(conn, merchant_id, intake):
        if not (_AREAS[sid] & sc.areas):
            areas = " or ".join(_TOPIC[a] for a in F.ALL_AREAS if a in _AREAS[sid])
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
    "RETURN_STUCK": """SELECT DISTINCT s.merchant_id FROM subscriptions s
                       WHERE s.device_id IN (SELECT device_id FROM device_events WHERE event_type = 'RETURN_REQUESTED')
                         AND s.device_id NOT IN (SELECT device_id FROM device_events WHERE event_type = 'PICKED_UP'
                                                 UNION SELECT device_id FROM pickup_tickets WHERE status = 'COMPLETED')""",
    "DUPLICATE_SUBSCRIPTION": """SELECT DISTINCT merchant_id FROM subscriptions WHERE status = 'ACTIVE' AND device_id IS NOT NULL
                                 GROUP BY merchant_id, device_id HAVING COUNT(*) > 1""",
    "DUPLICATE_EDI": """SELECT DISTINCT l.merchant_id FROM ledger_entries l LEFT JOIN edi_attempts a ON a.ledger_entry_id = l.id
                        WHERE l.entry_type = 'EDI_DEBIT' AND l.status = 'SUCCESS' AND l.direction = 'DEBIT'
                        GROUP BY l.merchant_id, l.source_id, COALESCE(a.due_date, l.value_date) HAVING COUNT(*) > 1""",
    "BUS_TRIP_FAILED": """SELECT DISTINCT b.passenger_id FROM bookings b JOIN trips t ON t.id = b.trip_id
                          WHERE t.kind = 'BUS' AND t.id IN (SELECT trip_id FROM trip_events WHERE event_type = 'RETURNED_TO_ORIGIN')""",
    "AIRLINE_REFUND_NOT_PASSED": """SELECT DISTINCT b.passenger_id FROM bookings b JOIN trips t ON t.id = b.trip_id
                                    WHERE t.kind = 'FLIGHT' AND t.status = 'CANCELLED'""",
}


def find_victims(conn, suspect_id: str, exclude_merchant_id: str | None, today: str) -> list[SuspectResult]:
    """Run one sweepable suspect's test across the fleet. Only MATCHes with damages > 0, merchant_id set, sorted by id."""
    spec = CATALOGUE.get(suspect_id)
    if not spec or not spec.sweepable or suspect_id not in _CANDIDATES:
        return []
    ctx = {"today_d": _today(today), "areas": set(F.ALL_AREAS), "disputed_ledger": [], "intake": None}
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
