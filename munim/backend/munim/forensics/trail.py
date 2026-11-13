"""The trail: a readable chain of custody around the disputed money, followed via foreign keys.

Disputed money movements ("the body") are chosen from the intake claims (tolerant of vague or missing dates); every row
linked to them is added with a short label, flags and a verb-labelled edge. Routine history is folded into summary nodes
("72 other EDI debits on LN-1003 (...), all single") so the graph stays at most ~45 nodes.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta

from munim.forensics import facts as F
from munim.models import ClaimKind, IntakeResult, Trail, TrailEdge, TrailNode, rupees

K = ClaimKind
MAX_NODES = 45
MAX_DISPUTED = 12
LEDGER_COLS = ("id", "merchant_id", "txn_ref", "entry_type", "direction", "amount_paise", "status", "source_type", "source_id",
               "mandate_id", "value_date", "posted_at", "narration", "refund_of")
ATTEMPT_COLS = ("id", "loan_id", "due_date", "attempt_no", "gateway_ref", "status", "responded_at", "ledger_entry_id")


def _pick(row: dict, cols: tuple[str, ...]) -> dict:
    return {k: row[k] for k in cols if k in row}


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, TrailNode] = {}
        self.prio: dict[str, int] = {}
        self.edges: list[TrailEdge] = []
        self._edge_keys: set[tuple[str, str]] = set()
        self.disputed: list[str] = []

    def node(self, table: str, row_id, kind: str, label: str, *, at=None, amount=None, flags=(), data=None, prio: int = 3) -> str:
        nid = f"{table}:{row_id}"
        if nid in self.nodes:
            n = self.nodes[nid]
            n.flags = list(dict.fromkeys([*n.flags, *flags]))
            self.prio[nid] = min(self.prio[nid], prio)
            return nid
        self.nodes[nid] = TrailNode(id=nid, table=table, row_id=str(row_id), kind=kind, label=label, at=at, amount_paise=amount,
                                    flags=list(dict.fromkeys(flags)), data=data or {})
        self.prio[nid] = prio
        return nid

    def flag(self, nid: str, *flags: str) -> None:
        if nid in self.nodes:
            n = self.nodes[nid]
            n.flags = list(dict.fromkeys([*n.flags, *flags]))

    def edge(self, source: str | None, target: str | None, label: str) -> None:
        if source and target and source != target and (source, target) not in self._edge_keys:
            self._edge_keys.add((source, target))
            self.edges.append(TrailEdge(source=source, target=target, label=label))

    def dispute(self, nid: str) -> None:
        self.flag(nid, "DISPUTED")
        self.prio[nid] = min(self.prio.get(nid, 1), 1)
        if nid not in self.disputed:
            self.disputed.append(nid)

    def build(self, merchant_id: str, today: date) -> Trail:
        order = list(self.nodes)
        rank = {nid: i for i, nid in enumerate(order)}
        keep = set(sorted(order, key=lambda n: (self.prio[n], rank[n]))[:MAX_NODES])
        nodes = [self.nodes[n] for n in order if n in keep]
        dates = sorted(d for d in (F.as_date(n.at) for n in nodes if n.kind != "merchant") if d)
        start = dates[0] if dates else today - timedelta(days=30)
        end = max(dates[-1], today) if dates else today
        return Trail(merchant_id=merchant_id, window_start=start.isoformat(), window_end=end.isoformat(),
                     disputed=[d for d in self.disputed if d in keep], nodes=nodes,
                     edges=[e for e in self.edges if e.source in keep and e.target in keep])


# ───────────────────────────── shared node makers ─────────────────────────────

def _ledger_node(b: _Builder, r: dict, prio: int, flags=()) -> str:
    return b.node("ledger_entries", r["id"], "money", F.ledger_label(r), at=r["posted_at"], amount=r["amount_paise"],
                  flags=flags, data=_pick(r, LEDGER_COLS), prio=prio)


def _summary_node(b: _Builder, rows: list[dict], label: str, prio: int = 4, flags=()) -> str | None:
    if not rows:
        return None
    if len(rows) <= 2:
        ids = [_ledger_node(b, r, prio, flags) for r in rows]
        return ids[-1]
    ids = [r["id"] for r in rows]
    return b.node("ledger_entries", f"{ids[0]}..{ids[-1]}", "money", label, at=rows[-1]["posted_at"],
                  amount=sum(r["amount_paise"] for r in rows), flags=("SUMMARY", *flags),
                  data={"count": len(rows), "ledger_ids": ids[:200]}, prio=prio)


def _refund_nodes(conn, b: _Builder, debit_nid: str, debit: dict) -> None:
    if not debit.get("refund_id"):
        return
    b.flag(debit_nid, "ALREADY_REFUNDED")
    ref = F.ledger_row(conn, debit["refund_id"])
    if ref:
        rid = _ledger_node(b, ref, 3, ("REFUND",))
        b.edge(rid, debit_nid, "refunds")


def _mandate_node(b: _Builder, m: dict | None) -> str | None:
    if not m:
        return None
    return b.node("mandates", m["id"], "mandate", f"Mandate {m['id']} · UMRN {m['umrn']} · up to {rupees(m['max_amount_paise'])} · {m['status']}",
                  at=m["created_at"], data=m, prio=2, flags=("REVOKED",) if m["status"] == "REVOKED" else ())


def _claims(intake: IntakeResult | None, *kinds: ClaimKind):
    return F.claims_of(intake, *kinds)


# ───────────────────────────── rent ─────────────────────────────

def _disputed_rent(debits: list[dict], intake, strict: bool, today: date, return_start: str | None) -> list[dict]:
    dated = [(c, F.claim_period(c.date, today)) for c in _claims(intake, K.DEDUCTION_AMOUNT, K.DUPLICATE_CHARGE, K.REFUND_DEMANDED)]
    dated = [(c, p) for c, p in dated if p]
    ret = next(iter(_claims(intake, K.DEVICE_RETURNED)), None)
    dup = bool(_claims(intake, K.DUPLICATE_CHARGE))
    count = next((c.count for c in _claims(intake, K.DEDUCTION_COUNT) if c.count), None)
    amount = next((c.amount_paise for c in _claims(intake, K.DEDUCTION_AMOUNT) if c.amount_paise), None)
    trim = False
    if dated:
        out = [d for d in debits if any(F.in_period(d["value_date"], p) for _, p in dated)]
    elif ret is not None:
        p = F.claim_period(ret.date, today)
        start = p[0].isoformat() if p else (return_start or (today - timedelta(days=90)).isoformat())
        out = [d for d in debits if d["value_date"] >= start]
        trim = True
    elif dup:
        months = Counter(d["value_date"][:7] for d in debits)
        out = [d for d in debits if months[d["value_date"][:7]] > 1]
    elif not strict:
        out = [d for d in debits if d["value_date"] >= (today - timedelta(days=90)).isoformat()]
        trim = True
    elif amount:
        out = [d for d in debits if F.amount_eq(amount, d["amount_paise"]) and d["value_date"] >= (today - timedelta(days=60)).isoformat()]
    else:
        out = []
    if trim and count and count > 0:
        out = out[-count:]
    if amount:
        same = [d for d in out if F.amount_eq(amount, d["amount_paise"])]
        out = same or out
    return out[-MAX_DISPUTED:]


def _rent(conn, b: _Builder, root: str, mid: str, intake, strict: bool, today: date) -> None:
    subs = F.subscriptions(conn, mid)
    if not subs:
        return
    zs = {z.sub["id"]: z for z in F.zombie_analysis(conn, mid)}
    groups = F.duplicate_subscriptions(conn, mid)
    later_ids = {s["id"] for g in groups for s in g.later}
    dup_extra = {d["id"] for g in groups for d in g.extra}
    starts = [z.lc.request["at"][:10] if z.lc.request else z.lc.pickup_date for z in zs.values() if z.lc and (z.lc.request or z.lc.pickup_date)]
    debits = F.rent_debits(conn, mid)
    disputed = _disputed_rent(debits, intake, strict, today, min(starts) if starts else None)
    disputed_ids = {d["id"] for d in disputed}
    wrong_ids = {d["id"] for z in zs.values() for d in z.after} | {d["id"] for g in groups for s in g.later if s["status"] == "ACTIVE"
                                                                      for d in g.extra}

    rel = [s for s in subs if any(d["source_id"] == s["id"] for d in disputed)]
    rel += [s for s in subs if s not in rel and (s["status"] == "ACTIVE" or s["id"] in later_ids)]
    rel = (rel or subs)[:4]
    devices_done: set[str] = set()
    for s in rel:
        z = zs.get(s["id"])
        lc = z.lc if z else None
        flags = []
        if lc and lc.pickup_at and s["status"] == "ACTIVE":
            flags.append("ACTIVE_AFTER_RETURN")
        if s["id"] in later_ids:
            flags.append("DUPLICATE")
        snid = b.node("subscriptions", s["id"], "subscription", F.sub_label(s), at=s["started_at"], flags=flags,
                      data={k: s[k] for k in ("id", "merchant_id", "device_id", "plan_id", "status", "started_at", "ended_at", "end_reason")},
                      prio=2)
        b.edge(root, snid, "subscribed")
        mnid = _mandate_node(b, F.mandate_for(conn, subscription_id=s["id"]))
        b.edge(mnid, snid, "authorised for")
        collect_to = mnid or snid
        collect_label = "collected via" if mnid else "billed for"

        dnid = None
        if s["device_id"]:
            dev = F._one(conn, "SELECT * FROM devices WHERE id=?", (s["device_id"],)) or {"id": s["device_id"], "serial_no": s["serial_no"],
                                                                                          "model": s["device_model"], "status": s["device_status"],
                                                                                          "merchant_id": s["device_merchant_id"]}
            unmapped = " · unmapped" if not dev.get("merchant_id") else ""
            dnid = b.node("devices", dev["id"], "device", f"Soundbox {dev['id']} · {dev['serial_no']} · {dev['model']} · {dev['status']}{unmapped}",
                          at=dev.get("installed_at"), data=dev, prio=2)
            b.edge(snid, dnid, "also billed" if s["id"] in later_ids else "rents")

        # disputed + wrongful debits individually, the rest folded into one summary node
        mine = [d for d in debits if d["source_id"] == s["id"]]
        for d in mine:
            if d["id"] not in disputed_ids and d["id"] not in wrong_ids:
                continue
            fl = []
            if lc and lc.pickup_date and d["value_date"] > lc.pickup_date:
                fl.append("AFTER_PICKUP")
            if d["id"] in dup_extra:
                fl.append("DUPLICATE")
            nid = _ledger_node(b, d, 1 if d["id"] in disputed_ids else 2, fl)
            if d["id"] in disputed_ids:
                b.dispute(nid)
            b.edge(nid, collect_to, collect_label)
            _refund_nodes(conn, b, nid, d)
        rest = [d for d in mine if d["id"] not in disputed_ids and d["id"] not in wrong_ids]
        if rest:
            amt = (f"{rupees(rest[0]['amount_paise'])} each" if len({d['amount_paise'] for d in rest}) == 1
                   else f"{rupees(sum(d['amount_paise'] for d in rest))} in total")
            note = ""
            if lc and lc.pickup_date and all(d["value_date"] <= lc.pickup_date for d in rest):
                note = ", all before the pickup"
            elif disputed and all(d["value_date"] < min(x["value_date"] for x in disputed) for d in rest):
                note = ", all before the disputed period"
            label = (f"{F.plural(len(rest), 'other rent debit')} on {s['id']} ({F.fspan(rest[0]['value_date'], rest[-1]['value_date'])}), "
                     f"{amt}{note}")
            sn = _summary_node(b, rest, label)
            b.edge(sn, collect_to, collect_label)

        if not dnid or s["device_id"] in devices_done:
            continue
        devices_done.add(s["device_id"])
        _device_story(conn, b, dnid, s, lc, mid, today)


def _device_story(conn, b: _Builder, dnid: str, s: dict, lc, mid: str, today: date) -> None:
    if lc is None:
        lc = F.lifecycle(conn, s["device_id"], mid, s["started_at"])
    important = {"RETURN_REQUESTED", "PICKUP_FAILED", "PICKED_UP", "RECEIVED_AT_WAREHOUSE", "DEACTIVATED", "PICKUP_SCHEDULED"}
    events = lc.events
    if len(events) > 8:
        events = [e for e in events if e["event_type"] in important][-8:]
    prev = dnid
    req_nid = None
    for e in events:
        fl = ("FAILED",) if e["event_type"] == "PICKUP_FAILED" else ()
        nid = b.node("device_events", e["id"], "device_event", F.event_label(e), at=e["at"], flags=fl, data=e,
                     prio=4 if e["event_type"] in ("INSTALLED", "SWAPPED_IN") else 3)
        b.edge(prev, nid, "logged" if prev == dnid else "then")
        prev = nid
        if e["event_type"] == "RETURN_REQUESTED":
            req_nid = nid
    if lc.ticket:
        t = lc.ticket
        tnid = b.node("pickup_tickets", t["id"], "pickup", F.ticket_label(t), at=t.get("completed_at") or t["requested_at"],
                      flags=("FAILED",) if t["status"] == "FAILED" else (), data=t, prio=3)
        b.edge(req_nid or dnid, tnid, "raised" if req_nid else "picked up via")

    # proof of (non-)usage: payments the device processed
    window = max(s["started_at"][:10], (today - timedelta(days=90)).isoformat())
    if lc.request and not lc.pickup_at:
        pay = F.payments_via(conn, s["device_id"], mid, after=lc.request["at"])
        label = (f"{F.plural(pay.count, 'payment')} via {s['device_id']} since the {F.fday(lc.request['at'])} return request "
                 f"({F.fspan(pay.first, pay.last)}), last {F.fdate(pay.last)} {F.fhm(pay.last)}" if pay.count
                 else f"No payments via {s['device_id']} since the {F.fdate(lc.request['at'])} return request")
        flags = ("USED_AFTER_RETURN_REQUEST",) if pay.count else ()
    elif lc.pickup_at:
        pay = F.payments_via(conn, s["device_id"], mid, after=window, before=lc.pickup_at)
        after = F.payments_via(conn, s["device_id"], mid, after=lc.pickup_at)
        tail = (f"; none after the {F.fday(lc.pickup_at)} pickup" if not after.count
                else f"; {F.plural(after.count, 'payment')} after the pickup")
        label = (f"{F.plural(pay.count, 'payment')} via {s['device_id']} ({F.fspan(pay.first, pay.last)}), last {F.fdate(pay.last)} "
                 f"{F.fhm(pay.last)}{tail}" if pay.count else f"No payments via {s['device_id']} before the pickup{tail}")
        flags = ()
    else:
        pay = F.payments_via(conn, s["device_id"], mid, after=(today - timedelta(days=30)).isoformat())
        label = (f"{F.plural(pay.count, 'payment')} via {s['device_id']} in the last 30 days ({F.fspan(pay.first, pay.last)})"
                 if pay.count else f"No payments via {s['device_id']} in the last 30 days")
        flags = ()
    pnid = b.node("payments", pay.last_id or f"none-{s['device_id']}", "payments_summary", label, at=pay.last, amount=pay.total or None,
                  flags=("SUMMARY", *flags), data={"count": pay.count, "first": pay.first, "last": pay.last, "total_paise": pay.total},
                  prio=3)
    b.edge(dnid, pnid, "processed")


# ───────────────────────────── loans ─────────────────────────────

def _loan(conn, b: _Builder, root: str, mid: str, intake, strict: bool, today: date) -> None:
    les = F.edi_analysis(conn, mid)
    if not les:
        return
    dated = [p for p in (F.claim_period(c.date, today) for c in _claims(intake, K.DUPLICATE_CHARGE, K.DEDUCTION_AMOUNT, K.REFUND_DEMANDED)) if p]
    amount = next((c.amount_paise for c in _claims(intake, K.DEDUCTION_AMOUNT) if c.amount_paise), None)
    for le in les[:2]:
        loan = le.loan
        rows = sorted((d for v in le.by_due.values() for d in v), key=lambda d: (d["posted_at"], d["id"]))
        if not rows and not le.loan["status"] == "ACTIVE":
            continue
        extra = {d["id"] for d in le.extra}
        if dated:
            disputed = [d for d in rows if any(F.in_period(d["due_date"] or d["value_date"], p) or F.in_period(d["value_date"], p) for p in dated)]
        elif le.dup_days:
            disputed = [d for v in le.dup_days.values() for d in v]
        elif not strict:
            disputed = rows[-3:]
        else:
            disputed = []
        if amount:
            disputed = [d for d in disputed if F.amount_eq(amount, d["amount_paise"])] or disputed
        disputed = disputed[-MAX_DISPUTED:]
        dis_ids = {d["id"] for d in disputed}

        lnid = b.node("loans", loan["id"], "loan",
                      f"Loan {loan['id']} · {loan['lender']} · {rupees(loan['principal_paise'])} · EDI {rupees(loan['edi_amount_paise'])}/day · "
                      f"since {F.fdate(loan['disbursed_at'])} · {loan['status']}", at=loan["disbursed_at"], data=loan, prio=2)
        b.edge(root, lnid, "borrowed")
        mnid = _mandate_node(b, F.mandate_for(conn, loan_id=loan["id"]))
        b.edge(mnid, lnid, "authorised for")
        collect_to = mnid or lnid

        show = [d for d in rows if d["id"] in dis_ids or d["id"] in extra]
        for d in show:
            fl = []
            if d["id"] in extra:
                fl.append("DUPLICATE")
            if d.get("attempt_status") == "TIMEOUT":
                fl.append("TIMEOUT_BUT_DEBITED")
            nid = _ledger_node(b, d, 1 if d["id"] in dis_ids else 2, fl)
            if d["id"] in dis_ids:
                b.dispute(nid)
            b.edge(nid, collect_to, "collected via")
            _refund_nodes(conn, b, nid, d)
        for due in sorted({d["due_date"] for d in show if d.get("due_date")}):
            prev = lnid
            for a in F.attempts_for(conn, loan["id"], due):
                fl = []
                if a["status"] == "TIMEOUT" and a["ledger_entry_id"]:
                    fl.append("TIMEOUT_BUT_DEBITED")
                elif a["status"] in ("FAILED", "TIMEOUT"):
                    fl.append(a["status"])
                anid = b.node("edi_attempts", a["id"], "edi_attempt",
                              f"EDI {F.fday(due)} · attempt {a['attempt_no']} · {a['status']} at {F.ftime(a['responded_at'])} · {a['gateway_ref']}",
                              at=a["responded_at"], flags=fl, data=_pick(a, ATTEMPT_COLS), prio=2)
                b.edge(prev, anid, "attempted" if prev == lnid else "retried")
                prev = anid
                if a["ledger_entry_id"]:
                    b.edge(anid, f"ledger_entries:{a['ledger_entry_id']}", "posted")
        rest = [d for d in rows if d["id"] not in dis_ids and d["id"] not in extra]
        if rest:
            other_dups = len([k for k, v in le.dup_days.items() if not any(x["id"] in dis_ids for x in v)])
            tail = "all single" if not other_dups else f"{F.plural(other_dups, 'other day')} debited more than once"
            label = (f"{F.plural(len(rest), 'other EDI debit')} on {loan['id']} ({F.fspan(rest[0]['value_date'], rest[-1]['value_date'])}), "
                     f"{tail}")
            sn = _summary_node(b, rest, label)
            b.edge(sn, collect_to, "collected via")


# ───────────────────────────── settlements ─────────────────────────────

def _disputed_stl(stls: list[dict], intake, strict: bool, today: date) -> list[dict]:
    claims = _claims(intake, K.SETTLEMENT_SHORT, K.SETTLEMENT_NOT_RECEIVED)
    out: list[dict] = []
    for c in claims:
        p = F.claim_period(c.date, today)
        pick: list[dict] = []
        if p:
            pick = [s for s in stls if F.in_period(s["batch_date"], p)]
        if not pick and c.amount_paise:
            pick = [s for s in stls if any(F.amount_eq(c.amount_paise, v) for v in
                                           (s["net_paise"], s["gross_paise"], s["deductions_paise"], s["gross_paise"] - s["net_paise"]))]
            pick = pick[-1:] if c.kind == K.SETTLEMENT_SHORT else pick
        if not pick and not p:
            if c.kind == K.SETTLEMENT_NOT_RECEIVED:
                held = [s for s in stls if s["status"] == "HELD"]
                pick = held[-c.count:] if (held and c.count) else (held or stls[-1:])
            else:
                odd = [s for s in stls if s["other_deductions"] > 0]
                pick = odd[-1:] or stls[-1:]
        out += [s for s in pick if s not in out]
    if not claims and not strict:
        out = [s for s in stls if s["status"] == "HELD"] or stls[-1:]
    return out[-7:]


def _settlement(conn, b: _Builder, root: str, mid: str, intake, strict: bool, today: date) -> None:
    stls = F.settlements(conn, mid, (today - timedelta(days=30)).isoformat())
    if not stls:
        return
    disputed = _disputed_stl(stls, intake, strict, today)
    dis_ids = {s["id"] for s in disputed}
    for s in disputed:
        fl = []
        if s["status"] == "HELD":
            fl.append("HELD")
        if s["other_deductions"] > 0:
            fl.append("UNEXPLAINED_DEDUCTION")
        snid = b.node("settlements", s["id"], "settlement", F.stl_label(s), at=s.get("settled_at") or s["batch_date"],
                      amount=s["net_paise"], flags=fl, data=s, prio=1)
        b.dispute(snid)
        b.edge(snid, root, "payout to")
        modes = conn.execute("SELECT mode, COUNT(*) FROM payments WHERE settlement_id=? GROUP BY mode ORDER BY mode", (s["id"],)).fetchall()
        last = conn.execute("SELECT id, paid_at FROM payments WHERE settlement_id=? ORDER BY paid_at DESC, id DESC LIMIT 1", (s["id"],)).fetchone()
        if last:
            mix = ", ".join(f"{n} {m}" for m, n in modes)
            pnid = b.node("payments", last[0], "payments_summary",
                          f"{F.plural(s['payment_count'], 'payment')} ({mix}) batched into {s['id']} · gross {rupees(s['gross_paise'])} · "
                          f"MDR {rupees(s['mdr_paise'])}", at=last[1], amount=s["gross_paise"], flags=("SUMMARY",),
                          data={"count": s["payment_count"], "modes": {m: n for m, n in modes}, "mdr_paise": s["mdr_paise"]}, prio=2)
            b.edge(pnid, snid, "batched")
        credit = F._one(conn, "SELECT * FROM ledger_entries WHERE merchant_id=? AND entry_type='SETTLEMENT_CREDIT' AND source_type='settlement' "
                              "AND source_id=? ORDER BY id LIMIT 1", (mid, s["id"]))
        if credit:
            b.edge(snid, _ledger_node(b, credit, 2), "paid out as")
        for h in F.holds_for_settlement(conn, s):
            hnid = b.node("holds", h["id"], "hold", F.hold_label(h) + f" · {h['reason_text']}", at=h["placed_at"],
                          flags=("ACTIVE_HOLD",) if h["status"] == "ACTIVE" else (), data=h, prio=2)
            b.edge(snid, hnid, "held by")
            if h["hold_type"] == "KYC":
                for d in F.kyc_docs(conn, mid):
                    if d["status"] in ("EXPIRED", "PENDING", "REJECTED"):
                        knid = b.node("kyc_documents", d["id"], "kyc", F.kyc_label(d), at=d.get("expires_at") or d.get("verified_at"),
                                      flags=(d["status"],), data=d, prio=2)
                        b.edge(hnid, knid, "because")
    rest = [s for s in stls if s["id"] not in dis_ids]
    if rest:
        held = sum(1 for s in rest if s["status"] == "HELD")
        tail = "all paid out" if not held else f"{held} held"
        label = (f"{F.plural(len(rest), 'other settlement')} ({F.fspan(rest[0]['batch_date'], rest[-1]['batch_date'])}), "
                 f"{rupees(sum(s['net_paise'] for s in rest))} net, {tail}")
        sn = b.node("settlements", f"{rest[0]['id']}..{rest[-1]['id']}", "settlement", label, at=rest[-1]["batch_date"],
                    amount=sum(s["net_paise"] for s in rest), flags=("SUMMARY",),
                    data={"count": len(rest), "settlement_ids": [s["id"] for s in rest]}, prio=4)
        b.edge(sn, root, "payout to")


# ───────────────────────────── Munim's own actions ─────────────────────────────

def _actions(conn, b: _Builder, mid: str) -> None:
    try:
        acts = F._rows(conn, "SELECT id, action_type, amount_paise, params_json, decision, status, created_at, executed_at FROM actions "
                             "WHERE merchant_id=? AND status IN ('EXECUTED','PENDING_APPROVAL') ORDER BY id DESC LIMIT 8", (mid,))
    except Exception:
        return
    for a in reversed(acts):
        try:
            params = json.loads(a["params_json"] or "{}")
        except ValueError:
            params = {}
        links: list[tuple[str, str]] = []
        if a["action_type"] == "REFUND_DEBITS":
            for r in conn.execute("SELECT id FROM ledger_entries WHERE source_type='action' AND source_id=?", (str(a["id"]),)):
                links.append((f"ledger_entries:{r[0]}", "issued"))
            if a["status"] == "PENDING_APPROVAL":
                links += [(f"ledger_entries:{i}", "would refund") for i in params.get("ledger_entry_ids") or []]
        else:
            tgt = params.get("target_id")
            table = {"CANCEL_SUBSCRIPTION": "subscriptions", "SCHEDULE_PICKUP": "devices", "SEND_KYC_LINK": "holds",
                     "RELEASE_HOLD": "holds"}.get(a["action_type"])
            verb = {"CANCEL_SUBSCRIPTION": "cancelled", "SCHEDULE_PICKUP": "booked pickup for", "SEND_KYC_LINK": "sent re-KYC for",
                    "RELEASE_HOLD": "released"}.get(a["action_type"], "acted on")
            if table and tgt:
                links.append((f"{table}:{tgt}", verb))
        links = [(t, v) for t, v in links if t in b.nodes]
        if not links:
            continue
        amt = f" {rupees(a['amount_paise'])}" if a["amount_paise"] else ""
        nid = b.node("actions", a["id"], "action", f"Munim action #{a['id']} · {a['action_type']}{amt} · {a['status']}",
                     at=a["executed_at"] or a["created_at"], amount=a["amount_paise"] or None, data={k: a[k] for k in a if k != "params_json"},
                     prio=3)
        for t, v in links:
            b.edge(nid, t, v)


# ───────────────────────────── public API ─────────────────────────────

def build_trail(conn, merchant_id: str, intake: IntakeResult, today: str) -> Trail:
    """Chain of custody for the complaint: disputed money first, then everything linked to it (≤ ~45 nodes)."""
    from munim import config
    today_d = F.as_date(today) or F.as_date(config.DEMO_TODAY) or date.today()
    b = _Builder()
    m = F._one(conn, "SELECT * FROM merchants WHERE id=?", (merchant_id,))
    if not m:
        return Trail(merchant_id=merchant_id, window_start=today_d.isoformat(), window_end=today_d.isoformat())
    root = b.node("merchants", m["id"], "merchant", f"{m['id']} · {m['business_name']} · {m['owner_name']} · {m['city']}",
                  at=m["onboarded_at"], data={k: m[k] for k in ("id", "business_name", "owner_name", "city", "category", "status")}, prio=0)
    sc = F.scope(conn, merchant_id, intake)
    if F.RENT in sc.areas:
        _rent(conn, b, root, merchant_id, intake, F.RENT in sc.strict, today_d)
    if F.LOAN in sc.areas:
        _loan(conn, b, root, merchant_id, intake, F.LOAN in sc.strict, today_d)
    if F.SETTLEMENT in sc.areas:
        _settlement(conn, b, root, merchant_id, intake, F.SETTLEMENT in sc.strict, today_d)
    _actions(conn, b, merchant_id)
    return b.build(merchant_id, today_d)
