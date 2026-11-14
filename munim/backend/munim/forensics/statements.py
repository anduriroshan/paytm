"""Witness statements: each intake claim checked against the ledger and the trail.

VERIFIED / CONTRADICTED / PARTLY_TRUE / UNVERIFIABLE, with one plain-English finding and evidence rows. Claims are never
trusted: amounts, counts and dates are compared with rows, and vague dates ("2026-09", "September", missing) are tolerated.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from munim.forensics import facts as F
from munim.models import Claim, ClaimKind, EvidenceRef, IntakeResult, StatementCheck, Trail, rupees

K = ClaimKind


class _Ctx:
    def __init__(self, conn, mid: str, intake: IntakeResult, trail: Trail | None, today: date) -> None:
        self.conn, self.mid, self.intake, self.trail, self.today = conn, mid, intake, trail, today
        self.scope = F.scope(conn, mid, intake)
        self.money_areas = self.scope.areas & {F.RENT, F.LOAN, F.TRAVEL}
        self._wrong: dict[int, str] | None = None
        self._disputed: list[dict] | None = None

    @property
    def wrong(self) -> dict[int, str]:
        if self._wrong is None:
            self._wrong = F.wrongful_debits(self.conn, self.mid, self.money_areas, include_refunded=True, today=self.today)
        return self._wrong

    @property
    def disputed(self) -> list[dict]:
        if self._disputed is None:
            self._disputed = F.debit_rows(self.conn, self.mid, F.disputed_ledger_ids(self.trail))
        return self._disputed

    def return_period(self) -> tuple[date, date] | None:
        return next((p for p in (F.claim_period(c.date, self.today) for c in F.claims_of(self.intake, K.DEVICE_RETURNED)) if p), None)


def _check(claim: Claim, status: str, finding: str, evidence: list[EvidenceRef] | None = None) -> StatementCheck:
    seen, ev = set(), []
    for e in evidence or []:
        if e.row_id and (e.table, e.row_id) not in seen:
            seen.add((e.table, e.row_id))
            ev.append(e)
    return StatementCheck(claim=claim, status=status, finding=finding, evidence=ev[:10])


def _each(rows: list[dict]) -> str:
    vals = sorted({r["amount_paise"] for r in rows})
    return f"{rupees(vals[0])}{' each' if len(rows) > 1 else ''}" if len(vals) == 1 else F.join_and([rupees(v) for v in vals])


def _kind_word(rows: list[dict]) -> str:
    kinds = {F._ENTRY.get(r["entry_type"], "debit") for r in rows}
    return next(iter(kinds)) if len(kinds) == 1 else "debit"


def _period_word(p: tuple[date, date]) -> str:
    if p[0] == p[1]:
        return F.fdate(p[0])
    if p[0].day == 1 and (p[1] + timedelta(days=1)).day == 1 and p[0].month == p[1].month:
        return F.fmonth(p[0])
    return F.fspan(p[0], p[1])


# ───────────────────────────── device ─────────────────────────────

def _device_returned(cx: _Ctx, c: Claim) -> StatementCheck:
    subs = [s for s in F.subscriptions(cx.conn, cx.mid) if s["device_id"]]
    if not subs:
        return _check(c, "UNVERIFIABLE", f"{cx.mid} has no rented Soundbox on record, so a return cannot be checked.")
    p = F.claim_period(c.date, cx.today)
    seen: set[str] = set()
    lcs = []
    for s in sorted(subs, key=lambda s: s["status"] != "ACTIVE"):
        if s["device_id"] not in seen:
            seen.add(s["device_id"])
            lcs.append((s, F.lifecycle(cx.conn, s["device_id"], cx.mid, s["started_at"])))

    picked = [(s, lc) for s, lc in lcs if lc.pickup_at]
    in_p = [(s, lc) for s, lc in picked if not p or F.in_period(lc.pickup_at, p)]
    if in_p:
        s, lc = in_p[0]
        awb = f" (AWB {lc.pickup_ref})" if lc.pickup_ref else ""
        recv = f", received at warehouse {F.fdate(lc.received['at'])}" if lc.received else ""
        ev = ([F.event_ref(lc.pickup_event)] if lc.pickup_event else []) + ([F.ticket_ref(lc.ticket)] if lc.ticket else [])
        ev += [F.event_ref(lc.received)] if lc.received else []
        return _check(c, "VERIFIED", f"{s['device_id']} was picked up on {F.fdate(lc.pickup_at)}{awb}{recv}.", ev)

    stuck = [(s, lc) for s, lc in lcs if lc.request and not lc.pickup_at]
    stuck.sort(key=lambda x: not (p and F.in_period(x[1].request["at"], p)))
    if stuck:
        s, lc = stuck[0]
        pay = F.payments_via(cx.conn, s["device_id"], cx.mid, after=lc.request["at"])
        if lc.failed:
            fails = F.join_and([f"{F.fday(e['at'])} ({F.human_code(e['note'] or 'failed')})" for e in lc.failed])
            why = f"{'pickups' if len(lc.failed) > 1 else 'the pickup'} failed {fails}"
        else:
            why = "no courier has collected it"
        since = (f"{F.plural(pay.count, 'payment')} since, the last on {F.fdate(pay.last)}" if pay.count
                 else "no payments since")
        ev = [F.event_ref(lc.request)] + [F.event_ref(e) for e in lc.failed] + ([F.ticket_ref(lc.ticket)] if lc.ticket else [])
        merchant_side = [e for e in lc.failed if str(e["note"] or "").upper() in F.MERCHANT_SIDE_FAILURES]
        if not pay.count and not merchant_side:      # asked to return, stopped using it; the courier never came
            days = (cx.today - F.as_date(lc.request["at"])).days
            return _check(c, "PARTLY_TRUE", f"Return of {s['device_id']} requested {F.fdate(lc.request['at'])} and the Soundbox "
                                            f"has taken no payments since, but it is not collected yet ({why}, "
                                            f"{F.plural(days, 'day')} on).", ev)
        if pay.count:
            ev.append(F.payments_ref(pay, f"{F.plural(pay.count, 'payment')} via {s['device_id']} since the return request, "
                                          f"last {F.fdate(pay.last)} {F.fhm(pay.last)}"))
        return _check(c, "CONTRADICTED", f"Return of {s['device_id']} requested {F.fdate(lc.request['at'])} but never collected: "
                                         f"{why}; {since}.", ev)

    if picked:
        s, lc = picked[-1]
        awb = f" (AWB {lc.pickup_ref})" if lc.pickup_ref else ""
        return _check(c, "PARTLY_TRUE", f"{s['device_id']} was returned, but it was picked up on {F.fdate(lc.pickup_at)}{awb}, "
                                        f"not in {_period_word(p)}.",
                      ([F.event_ref(lc.pickup_event)] if lc.pickup_event else []) + ([F.ticket_ref(lc.ticket)] if lc.ticket else []))

    s, lc = lcs[0]
    pay = F.payments_via(cx.conn, s["device_id"], cx.mid, after=(cx.today - timedelta(days=30)).isoformat())
    use = (f"{F.plural(pay.count, 'payment')} went through it in the last 30 days, the last on {F.fdate(pay.last)}" if pay.count
           else f"it is still assigned to {cx.mid}")
    ev = [F.sub_ref(s)] + ([F.payments_ref(pay, f"{F.plural(pay.count, 'payment')} via {s['device_id']} in the last 30 days")] if pay.count else [])
    return _check(c, "CONTRADICTED", f"No return was ever requested for {s['device_id']}; {use}.", ev)


def _single_device(cx: _Ctx, c: Claim) -> StatementCheck:
    subs = F.subscriptions(cx.conn, cx.mid)
    active = [s for s in subs if s["status"] == "ACTIVE" and s["device_id"]]
    mapped = [r[0] for r in cx.conn.execute("SELECT id FROM devices WHERE merchant_id=? ORDER BY id", (cx.mid,))]
    devices = sorted(set(mapped) | {s["device_id"] for s in active})
    if not devices:
        return _check(c, "UNVERIFIABLE", f"No Soundbox is mapped to {cx.mid} or billed by an active subscription.")
    ev = [F.sub_ref(s) for s in active]
    if len(devices) == 1:
        dev = devices[0]
        on_it = [s for s in active if s["device_id"] == dev]
        tail = (f"; {F.plural(len(on_it), 'active subscription')} ({F.join_and([s['id'] for s in on_it])}) point to it"
                if len(on_it) > 1 else "")
        return _check(c, "VERIFIED", f"Only one Soundbox, {dev}, is on {cx.mid}'s account{tail}.", ev)
    return _check(c, "CONTRADICTED", f"{cx.mid} has {F.plural(len(devices), 'Soundbox', 'Soundboxes')} on record "
                                     f"({F.join_and(devices)}), not one.", ev)


# ───────────────────────────── debits ─────────────────────────────

def _recent_debits(cx: _Ctx, days: int = 90) -> list[dict]:
    types = [t for a, t in ((F.RENT, "RENT_DEBIT"), (F.LOAN, "EDI_DEBIT")) if a in cx.scope.areas] or ["RENT_DEBIT", "EDI_DEBIT"]
    marks = ",".join("?" * len(types))
    return F._rows(cx.conn, f"SELECT * FROM ledger_entries WHERE merchant_id=? AND direction='DEBIT' AND status='SUCCESS' "
                            f"AND entry_type IN ({marks}) AND value_date>=? ORDER BY value_date, id",
                   (cx.mid, *types, (cx.today - timedelta(days=days)).isoformat()))


def _deduction_amount(cx: _Ctx, c: Claim) -> StatementCheck:
    amt = c.amount_paise
    if not amt:
        return _check(c, "UNVERIFIABLE", "No amount was stated, so there is nothing to compare.")
    pool = cx.disputed
    hit = [d for d in pool if F.amount_eq(amt, d["amount_paise"])]
    if hit:
        word = _kind_word(hit)
        if len(hit) == 1:
            lead = f"The disputed {word} was" if len(pool) == 1 else f"1 of the {len(pool)} disputed debits was a"
        else:
            lead = ((f"Both disputed {word}s were" if len(hit) == 2 else f"All {len(hit)} disputed {word}s were")
                    if len(hit) == len(pool)
                    else f"{len(hit)} of the {len(pool)} disputed debits were {word}s of")
        amt = rupees(hit[0]["amount_paise"]) if len({d["amount_paise"] for d in hit}) == 1 else _each(hit)
        return _check(c, "VERIFIED", f"{lead} {amt} ({F.fdates(d['value_date'] for d in hit)}).", [F.ledger_ref(d) for d in hit])
    recent = [d for d in _recent_debits(cx) if F.amount_eq(amt, d["amount_paise"])]
    if recent:
        return _check(c, "VERIFIED", f"{F.plural(len(recent), _kind_word(recent))} of {rupees(recent[0]['amount_paise'])} "
                                     f"{F.was(len(recent))} taken in the last 90 days ({F.fdates(d['value_date'] for d in recent)}).",
                      [F.ledger_ref(d) for d in recent[-4:]])
    if F.SETTLEMENT in cx.scope.areas:
        stls = [s for s in F.settlements(cx.conn, cx.mid, (cx.today - timedelta(days=30)).isoformat())
                if F.amount_eq(amt, s["deductions_paise"])]
        if stls:
            s = stls[-1]
            return _check(c, "VERIFIED", f"{s['id']} ({F.fdate(s['batch_date'])}) had {rupees(s['deductions_paise'])} deducted.", [F.stl_ref(s)])
    if pool:
        return _check(c, "CONTRADICTED", f"The disputed debits were {_each(pool)}, not {rupees(amt)}.", [F.ledger_ref(d) for d in pool[-4:]])
    return _check(c, "CONTRADICTED", f"No debit of {rupees(amt)} was taken from {cx.mid} in the last 90 days.")


def _deduction_count(cx: _Ctx, c: Claim) -> StatementCheck:
    n = c.count
    pool = cx.disputed
    wrong = [d for d in pool if d["id"] in cx.wrong]
    if not pool:
        wrong = F.debit_rows(cx.conn, cx.mid, cx.wrong)
    if not n:
        return _check(c, "UNVERIFIABLE", f"No count was stated; the ledger shows {F.plural(len(wrong), 'wrongful debit')}.",
                      [F.ledger_ref(d) for d in wrong])
    p = cx.return_period()
    since = f"since {F.MONTH_FULL[p[0].month - 1]}" if p else "in the disputed period"
    ev = [F.ledger_ref(d) for d in (pool or wrong)]
    if wrong and len(wrong) == n:
        return _check(c, "VERIFIED", f"{F.plural(n, 'wrongful ' + _kind_word(wrong))} ({F.fdates(d['value_date'] for d in wrong)}, "
                                     f"{rupees(sum(d['amount_paise'] for d in wrong))}).", ev)
    if wrong:
        legit = [d for d in pool if d["id"] not in cx.wrong]
        total = rupees(sum(d["amount_paise"] for d in wrong))
        if pool and len(pool) >= n and legit:
            zombie = [z for z in F.zombie_analysis(cx.conn, cx.mid) if any(d["id"] in {x["id"] for x in z.after} for d in wrong)]
            if zombie and zombie[0].lc.pickup_at:
                why = f"{F.was(len(legit))} before the {F.fday(zombie[0].lc.pickup_at)} pickup"
                tail = f"only {len(wrong)} ({total}) came after"
            else:
                why = "{} legitimate".format(F.was(len(legit)))
                tail = f"only {len(wrong)} ({total}) {F.was(len(wrong))} wrong"
            return _check(c, "PARTLY_TRUE", f"{F.plural(len(pool), _kind_word(pool))} {since} ({F.fdates(d['value_date'] for d in pool)}), "
                                            f"but {F.fdates(d['value_date'] for d in legit)} {why}; {tail}.", ev)
        return _check(c, "PARTLY_TRUE", f"Only {F.plural(len(wrong), 'wrongful ' + _kind_word(wrong))} "
                                        f"({F.fdates(d['value_date'] for d in wrong)}, {total}), not {n}.", ev)
    if pool:
        return _check(c, "PARTLY_TRUE", f"{F.plural(len(pool), _kind_word(pool))} {since} ({F.fdates(d['value_date'] for d in pool)}), "
                                        f"but no test found any of them wrongful.", ev)
    return _check(c, "CONTRADICTED", f"No disputed debit was found {since}.")


def _duplicate_charge(cx: _Ctx, c: Claim) -> StatementCheck:
    p = F.claim_period(c.date, cx.today)
    checked = []
    if F.LOAN in cx.scope.areas:
        for le in F.edi_analysis(cx.conn, cx.mid):
            checked.append(f"loan {le.loan['id']}")
            days = {k: v for k, v in le.dup_days.items() if not p or F.in_period(k, p)}
            if days:
                ev = []
                if len(days) == 1:
                    day, rows = next(iter(days.items()))
                    parts = [f"{F.ftime(r['posted_at'])}" + (f" (attempt {r['attempt_no']}, {r['attempt_status']})" if r.get("attempt_no") else "")
                             for r in rows]
                    msg = (f"The {F.fdate(day)} EDI of {rupees(le.loan['edi_amount_paise'])} on {le.loan['id']} was debited "
                           f"{F.times_word(len(rows))}: at {F.join_and(parts)}.")
                else:
                    msg = (f"EDI on {le.loan['id']} was debited more than once on {F.plural(len(days), 'day')} "
                           f"({F.fdates(days)}).")
                for rows in days.values():
                    ev += [F.ledger_ref(r) for r in rows]
                return _check(c, "VERIFIED", msg, ev)
    if F.RENT in cx.scope.areas:
        debits = F.rent_debits(cx.conn, cx.mid)
        if debits:
            checked.append("rent")
            months = Counter(d["value_date"][:7] for d in debits if not p or F.in_period(d["value_date"], p) or
                             (p and d["value_date"][:7] == p[0].isoformat()[:7]))
            dup = sorted(m for m, k in months.items() if k > 1)
            if dup:
                rows = [d for d in debits if d["value_date"][:7] in dup]
                subs = list(dict.fromkeys(d["source_id"] for d in rows))
                firsts = [min(d["value_date"] for d in rows if d["value_date"][:7] == m) for m in dup]
                return _check(c, "VERIFIED", f"Rent was debited twice in {F.plural(len(dup), 'month')} ({F.fdates(firsts)}), "
                                             f"once each by {F.join_and(subs)}.", [F.ledger_ref(d) for d in rows[-6:]])
    if not checked:
        return _check(c, "UNVERIFIABLE", f"{cx.mid} has no rent or loan debits to compare.")
    when = f" on {_period_word(p)}" if p else ""
    return _check(c, "CONTRADICTED", f"No {F.join_and(checked)} debit was taken twice{when}; each due date has a single debit.")


# ───────────────────────────── settlements ─────────────────────────────

def _target_settlements(cx: _Ctx, c: Claim) -> list[dict]:
    stls = F.settlements(cx.conn, cx.mid, (cx.today - timedelta(days=30)).isoformat())
    p = F.claim_period(c.date, cx.today)
    if p:
        pick = [s for s in stls if F.in_period(s["batch_date"], p)]
        if pick:
            return pick
    if c.amount_paise:
        pick = [s for s in stls if any(F.amount_eq(c.amount_paise, v) for v in
                                       (s["net_paise"], s["gross_paise"], s["deductions_paise"], s["gross_paise"] - s["net_paise"]))]
        if pick:
            return pick[-1:]
    if c.kind == K.SETTLEMENT_NOT_RECEIVED:
        held = [s for s in stls if s["status"] == "HELD"]
        if held:
            return held[-c.count:] if c.count else held
        return stls[-(c.count or 1):]
    odd = [s for s in stls if s["other_deductions"] > 0]
    return odd[-1:] or stls[-1:]


def _settlement_short(cx: _Ctx, c: Claim) -> StatementCheck:
    stls = _target_settlements(cx, c)
    if not stls:
        return _check(c, "UNVERIFIABLE", f"No settlement batch for {cx.mid} in the last 30 days.")
    s = stls[-1]
    gap = s["gross_paise"] - s["net_paise"]
    unexplained = f", MDR {rupees(s['mdr_paise'])}" + (f", so {rupees(s['other_deductions'])} is not MDR" if s["other_deductions"] > 0 else "")
    breakdown = (f"{s['id']} ({F.fdate(s['batch_date'])}): gross {rupees(s['gross_paise'])} − net {rupees(s['net_paise'])} = "
                 f"{rupees(gap)} deducted{unexplained}")
    held = f"; the payout is HELD" if s["status"] == "HELD" else ""
    if not c.amount_paise:
        status = "VERIFIED" if gap > 0 else "CONTRADICTED"
        return _check(c, status, breakdown + held + ".", [F.stl_ref(s)])
    if F.amount_eq(c.amount_paise, gap) or F.amount_eq(c.amount_paise, s["deductions_paise"]):
        return _check(c, "VERIFIED", breakdown + held + ".", [F.stl_ref(s)])
    if gap > 0:
        return _check(c, "PARTLY_TRUE", f"{breakdown}, not {rupees(c.amount_paise)}{held}.", [F.stl_ref(s)])
    return _check(c, "CONTRADICTED", f"{s['id']} ({F.fdate(s['batch_date'])}) paid out the full {rupees(s['gross_paise'])} with no deduction.",
                  [F.stl_ref(s)])


def _settlement_not_received(cx: _Ctx, c: Claim) -> StatementCheck:
    stls = _target_settlements(cx, c)
    if not stls:
        when = f" for {_period_word(F.claim_period(c.date, cx.today))}" if F.claim_period(c.date, cx.today) else ""
        return _check(c, "UNVERIFIABLE", f"No settlement batch{when} was found for {cx.mid}.")
    held = [s for s in stls if s["status"] == "HELD"]
    paid = [s for s in stls if s["status"] != "HELD"]
    ev = [F.stl_ref(s) for s in stls]
    if held:
        hs = list({h["id"]: h for s in held for h in F.holds_for_settlement(cx.conn, s) if h["status"] == "ACTIVE"}.values())
        by = (f" by {F.join_and([F.lc_first(F.hold_label(h).split(' · ')[0]) for h in hs])} since {F.fdate(hs[0]['placed_at'])}" if hs else "")
        ev = [F.hold_ref(h) for h in hs] + ev
        verb = "is" if len(held) == 1 else "are"
        msg = f"{F.stl_phrase(held)} {verb} HELD{by}"
        amount_ok = not c.amount_paise or any(F.amount_eq(c.amount_paise, v) for v in
                                              [s["net_paise"] for s in held] + [sum(s["net_paise"] for s in held)])
        count_ok = not c.count or len(held) == c.count
        if paid:
            msg += f"; {F.plural(len(paid), 'other batch', 'other batches')} {F.was(len(paid))} paid out"
        if not amount_ok:
            msg += f"; the held amount is not {rupees(c.amount_paise)}"
        if not count_ok:
            msg += f"; {F.plural(len(held), 'batch', 'batches')} held, not {c.count}"
        return _check(c, "VERIFIED" if (amount_ok and count_ok and not paid) else "PARTLY_TRUE", msg + ".", ev)
    s = paid[-1]
    when = F.fdate(s["settled_at"] or s["batch_date"]) + (f" {F.fhm(s['settled_at'])}" if s.get("settled_at") else "")
    utr = f" (UTR {s['utr']})" if s.get("utr") else ""
    lead = f"{s['id']} ({rupees(s['net_paise'])} net) was paid out on {when}{utr}"
    if len(paid) > 1:
        lead = f"All {F.plural(len(paid), 'batch', 'batches')} were paid out; the latest, {lead[0].lower() + lead[1:]}"
    return _check(c, "CONTRADICTED", lead + ".", ev)


# ───────────────────────────── refund demand ─────────────────────────────

def _refund_demanded(cx: _Ctx, c: Claim) -> StatementCheck:
    open_ = F.debit_rows(cx.conn, cx.mid, F.wrongful_debits(cx.conn, cx.mid, cx.money_areas, include_refunded=False, today=cx.today))
    done = [d for d in F.debit_rows(cx.conn, cx.mid, cx.wrong) if d["id"] not in {o["id"] for o in open_}]
    owed = sum(d["amount_paise"] for d in open_)
    ev = [F.ledger_ref(d) for d in open_]
    unrefunded = (f"{F.plural(len(open_), 'wrongful ' + _kind_word(open_))} not yet refunded "
                  f"({F.fdates(d['value_date'] for d in open_)}, {rupees(owed)})") if open_ else ""
    supported = f"the ledger shows {unrefunded}" if open_ else ""
    if done and not open_:
        supported = f"the wrongful {F.plural(len(done), 'debit')} ({rupees(sum(d['amount_paise'] for d in done))}) {F.was(len(done))} already refunded"
    if not c.amount_paise:
        tail = supported or "the ledger shows no wrongful debit to refund"
        return _check(c, "UNVERIFIABLE", f"No amount was stated; {tail}.", ev)
    if open_ and F.amount_eq(c.amount_paise, owed):
        return _check(c, "VERIFIED", f"{rupees(c.amount_paise)} matches the {unrefunded}.", ev)
    if open_:
        return _check(c, "PARTLY_TRUE", f"{rupees(c.amount_paise)} demanded, but {supported}.", ev)
    tail = supported or "no debit in the ledger supports a refund"
    return _check(c, "CONTRADICTED", f"{rupees(c.amount_paise)} demanded, but {tail}.")


# ───────────────────────────── travel ─────────────────────────────

def _in_period(rows: list, at, p: tuple[date, date] | None) -> list:
    if not p:
        return rows
    hit = [r for r in rows if (d := F.as_date(at(r))) and p[0] <= d <= p[1]]
    return hit or rows


def _trip_not_completed(cx: _Ctx, c: Claim) -> StatementCheck:
    from munim.forensics import travel as T
    p = F.claim_period(c.date, cx.today)
    buses = _in_period(T.bus_checks(cx.conn, cx.mid), lambda x: x.booking["departs_at"], p)
    if buses:
        x = next((b for b in buses if b.failed), buses[-1])
        b = x.booking
        ev = [T.booking_ref(b), T.trip_ref(b)] + [T.event_ref(e, b) for e in (x.turned, x.report, x.back, x.arrived) if e]
        if x.failed:
            said = f"; the driver reported “{x.report['note'].rstrip('.')}”" if x.report else ""
            return _check(c, "VERIFIED", f"GPS shows bus {b['service_no']} turning back at {T.km(x.km)} and back at "
                                         f"{b['origin']} at {F.fhm(x.back['at'])} on {F.fday(x.back['at'])}{said}; it never "
                                         f"reached {b['destination']}, though the operator status still says {b['trip_status']}.", ev)
        if x.arrived:
            return _check(c, "CONTRADICTED", f"GPS shows bus {b['service_no']} reached {b['destination']} at "
                                             f"{F.fhm(x.arrived['at'])} on {F.fday(x.arrived['at'])}.", ev)
        return _check(c, "UNVERIFIABLE", f"No GPS or driver record shows bus {b['service_no']} turning back; the operator "
                                         f"status is {b['trip_status']}.", ev)
    flights = [f for f in T.flight_checks(cx.conn, cx.mid, cx.today) if f.cancelled]
    if flights:
        f = flights[-1]
        b = f.booking
        return _check(c, "VERIFIED", f"{b['operator_name']} cancelled flight {b['service_no']} ({T.route(b)}) on "
                                     f"{F.fdate(b['trip_status_at'])}.", [T.booking_ref(b)] + ([T.event_ref(f.cancel, b)] if f.cancel else []))
    return _check(c, "UNVERIFIABLE", f"{cx.mid} has no bus or flight booking on record, so the trip cannot be checked.")


def _refund_not_received(cx: _Ctx, c: Claim) -> StatementCheck:
    from munim.forensics import travel as T
    checks: list = [x for x in T.bus_checks(cx.conn, cx.mid) if x.fare] + \
                   [x for x in T.flight_checks(cx.conn, cx.mid, cx.today) if x.fare]
    if not checks:
        return _check(c, "UNVERIFIABLE", f"{cx.mid} has no ticket payment on record, so a refund cannot be checked.")
    if c.amount_paise:
        checks = [x for x in checks if F.amount_eq(c.amount_paise, x.fare["amount_paise"])] or checks
    at_issue = [x for x in checks if getattr(x, "failed", False) or getattr(x, "cancelled", False)] or checks
    x = at_issue[-1]
    b, fare = x.booking, x.fare
    paid = getattr(x, "paid", None)
    ev = [T.booking_ref(b), F.ledger_ref(fare)] + ([T.airline_refund_ref(paid, b["operator_name"])] if paid else [])
    if x.refunded:
        return _check(c, "CONTRADICTED", f"The {rupees(fare['amount_paise'])} fare for {b['id']} was refunded on "
                                         f"{F.fdate(fare['refund_date'])} ({fare['refund_ref']}).",
                      ev + [F.ledger_ref(F.ledger_row(cx.conn, fare["refund_id"]))])
    airline = (f"; {b['operator_name']} paid it back to us on {F.fdate(paid['received_at'])}, {x.days} days ago"
               if paid else "")
    finding = f"No refund of the {rupees(fare['amount_paise'])} fare for {b['id']} has been paid{airline}."
    if c.amount_paise and not F.amount_eq(c.amount_paise, fare["amount_paise"]):
        return _check(c, "PARTLY_TRUE", f"{rupees(c.amount_paise)} claimed, but the fare paid was {rupees(fare['amount_paise'])}. "
                                        + finding, ev)
    return _check(c, "VERIFIED", finding, ev)


_CHECKS = {K.DEVICE_RETURNED: _device_returned, K.SINGLE_DEVICE: _single_device, K.DEDUCTION_AMOUNT: _deduction_amount,
           K.DEDUCTION_COUNT: _deduction_count, K.DUPLICATE_CHARGE: _duplicate_charge, K.SETTLEMENT_SHORT: _settlement_short,
           K.SETTLEMENT_NOT_RECEIVED: _settlement_not_received, K.REFUND_DEMANDED: _refund_demanded,
           K.TRIP_NOT_COMPLETED: _trip_not_completed, K.REFUND_NOT_RECEIVED: _refund_not_received}


def check_statements(conn, merchant_id: str, intake: IntakeResult, trail: Trail, today: str) -> list[StatementCheck]:
    """One StatementCheck per intake claim, in claim order."""
    from munim import config
    today_d = F.as_date(today) or F.as_date(config.DEMO_TODAY) or date.today()
    if intake is None:
        return []
    cx = _Ctx(conn, merchant_id, intake, trail, today_d)
    out = []
    for c in intake.claims:
        fn = _CHECKS.get(c.kind)
        if fn is None:
            out.append(_check(c, "UNVERIFIABLE", "This is not a fact the ledger can confirm or refute."))
            continue
        try:
            out.append(fn(cx, c))
        except Exception as e:   # a malformed claim must not break the investigation
            out.append(_check(c, "UNVERIFIABLE", f"This claim could not be checked ({type(e).__name__})."))
    return out
