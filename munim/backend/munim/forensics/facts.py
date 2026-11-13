"""Shared, deterministic fact-finding for the forensics modules (private to munim.forensics).

Formatting (dates like "12 Sep 2026", ₹ via rupees()), tolerant claim parsing, complaint scoping, and the per-merchant
analyses that suspects, trail and statements all agree on (pickup lifecycle, rent/EDI debits with refunds, holds).
Every function takes an open connection and only reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from munim.models import Claim, ClaimKind, EvidenceRef, IntakeResult, Intent, rupees

RENT, LOAN, SETTLEMENT = "RENT", "LOAN", "SETTLEMENT"
AREAS = (RENT, LOAN, SETTLEMENT)
INTENT_AREA = {Intent.RENT_DEDUCTION: RENT, Intent.LOAN_DEDUCTION: LOAN, Intent.SETTLEMENT_ISSUE: SETTLEMENT}

MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
_MONTH_RX = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
             "nov": 11, "dec": 12}
NUM_WORD = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
DOC_LABEL = {"PAN": "PAN", "AADHAAR": "Aadhaar", "GSTIN": "GSTIN", "DRUG_LICENSE": "drug licence",
             "SHOP_LICENSE": "shop licence"}
EVENT_LABEL = {"INSTALLED": "Installed", "RETURN_REQUESTED": "Return requested", "PICKUP_SCHEDULED": "Pickup scheduled",
               "PICKUP_FAILED": "Pickup failed", "PICKED_UP": "Picked up", "RECEIVED_AT_WAREHOUSE": "Received at warehouse",
               "DEACTIVATED": "Deactivated", "SWAPPED_OUT": "Swapped out", "SWAPPED_IN": "Swapped in"}


# ───────────────────────────── formatting ─────────────────────────────

def as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def fdate(v: Any) -> str:
    d = as_date(v)
    return f"{d.day:02d} {MON[d.month - 1]} {d.year}" if d else "an unknown date"


def fday(v: Any) -> str:
    d = as_date(v)
    return f"{d.day:02d} {MON[d.month - 1]}" if d else "?"


def ftime(v: Any) -> str:
    s = str(v or "")
    return s[11:19] if len(s) >= 19 else (s[11:16] if len(s) >= 16 else "")


def fhm(v: Any) -> str:
    s = str(v or "")
    return s[11:16] if len(s) >= 16 else ""


def fmonth(d: date) -> str:
    return f"{MONTH_FULL[d.month - 1]} {d.year}"


def fdates(values: Iterable[Any], max_list: int = 4) -> str:
    """'01 Oct, 01 Nov 2026' · '01 Dec 2025, 01 Jan 2026' · '01 Jul – 01 Nov 2026' for long runs."""
    ds = sorted({d for d in (as_date(v) for v in values) if d})
    if not ds:
        return ""
    if len(ds) > max_list:
        return f"{fday(ds[0]) if ds[0].year == ds[-1].year else fdate(ds[0])} – {fdate(ds[-1])}"
    if len({d.year for d in ds}) == 1:
        return ", ".join(fday(d) for d in ds[:-1]) + (", " if len(ds) > 1 else "") + fdate(ds[-1])
    return ", ".join(fdate(d) for d in ds)


def fspan(a: Any, b: Any) -> str:
    da, db = as_date(a), as_date(b)
    if not da or not db:
        return fdate(da or db)
    if da == db:
        return fdate(da)
    return f"{fday(da) if da.year == db.year else fdate(da)} – {fdate(db)}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def num(n: int) -> str:
    return NUM_WORD[n] if 0 <= n < len(NUM_WORD) else str(n)


def times_word(n: int) -> str:
    return {1: "once", 2: "twice", 3: "three times"}.get(n, f"{n} times")


def was(n: int) -> str:
    return "was" if n == 1 else "were"


def human_code(code: str | None) -> str:
    return (code or "").replace("_", " ").lower()


def lc_first(s: str) -> str:
    return s[:1].lower() + s[1:] if len(s) > 1 and s[1:2].islower() else s


def join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ───────────────────────────── claim parsing ─────────────────────────────

def claim_period(raw: str | None, today: date) -> tuple[date, date] | None:
    """Tolerant date parsing for intake claims → inclusive (start, end). None when nothing usable."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in ("today", "aaj"):
        return today, today
    if s in ("yesterday", "kal"):
        y = today - timedelta(days=1)
        return y, y
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            return d, d
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})$", s) or re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m:
        y, mo = (int(m[1]), int(m[2])) if len(m[1]) == 4 else (int(m[2]), int(m[1]))
        return _month_range(y, mo) if 1 <= mo <= 12 else None
    m = re.search(r"(?:(\d{1,2})(?:st|nd|rd|th)?\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?(?:,?\s+(\d{4}))?", s)
    if m:
        mo = _MONTH_RX[m[2]]
        day = int(m[1] or m[3]) if (m[1] or m[3]) else None
        year = int(m[4]) if m[4] else (today.year if (mo, day or 1) <= (today.month, today.day) else today.year - 1)
        if day:
            try:
                d = date(year, mo, day)
                return d, d
            except ValueError:
                return None
        return _month_range(year, mo)
    return None


def _month_range(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return start, end


def in_period(v: Any, period: tuple[date, date] | None) -> bool:
    d = as_date(v)
    return bool(d and period and period[0] <= d <= period[1])


def amount_eq(claimed: int | None, actual: int | None) -> bool:
    """Intake amounts should be paise, but tolerate a rupee figure slipping through (499 vs 49900)."""
    if claimed is None or actual is None:
        return False
    return claimed == actual or claimed * 100 == actual


def claims_of(intake: IntakeResult | None, *kinds: ClaimKind) -> list[Claim]:
    if not intake:
        return []
    return [c for c in intake.claims if not kinds or c.kind in kinds]


def disputed_ids(trail: Any, table: str) -> list[str]:
    if not trail:
        return []
    pre = f"{table}:"
    return [d[len(pre):] for d in getattr(trail, "disputed", []) or [] if d.startswith(pre)]


def disputed_ledger_ids(trail: Any) -> list[int]:
    out = []
    for s in disputed_ids(trail, "ledger_entries"):
        try:
            out.append(int(s))
        except ValueError:
            pass
    return out


# ───────────────────────────── scope ─────────────────────────────

@dataclass
class Scope:
    areas: set[str]
    strict: set[str] = field(default_factory=set)   # areas reached only via OTHER/hints: dispute only explicit matches


def scope(conn, merchant_id: str, intake: IntakeResult | None) -> Scope:
    """Which money areas the complaint touches. OTHER → all areas; claims can widen a misclassified intent."""
    if intake is None:
        return Scope(set(AREAS))
    base = {INTENT_AREA[intake.intent]} if intake.intent in INTENT_AREA else set()
    hints: set[str] = set()
    fees = {r[0] for r in conn.execute(
        "SELECT p.monthly_fee_paise FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.merchant_id=?", (merchant_id,))}
    edis = {r[0] for r in conn.execute("SELECT edi_amount_paise FROM loans WHERE merchant_id=?", (merchant_id,))}
    for c in intake.claims:
        if c.kind in (ClaimKind.DEVICE_RETURNED, ClaimKind.SINGLE_DEVICE):
            hints.add(RENT)
        elif c.kind in (ClaimKind.SETTLEMENT_SHORT, ClaimKind.SETTLEMENT_NOT_RECEIVED):
            hints.add(SETTLEMENT)
        if c.amount_paise and c.kind in (ClaimKind.DEDUCTION_AMOUNT, ClaimKind.DUPLICATE_CHARGE, ClaimKind.REFUND_DEMANDED,
                                         ClaimKind.OTHER):
            if any(amount_eq(c.amount_paise, f) for f in fees):
                hints.add(RENT)
            if any(amount_eq(c.amount_paise, e) for e in edis):
                hints.add(LOAN)
    if not base:                                   # OTHER: test everything, dispute only what claims point at
        return Scope(set(AREAS), set(AREAS) - hints)
    return Scope(base | hints, hints - base)


# ───────────────────────────── rows ─────────────────────────────

def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


_DEBIT_SQL = """
SELECT l.*, r.id AS refund_id, r.value_date AS refund_date, r.txn_ref AS refund_ref, r.posted_at AS refund_posted_at,
       r.source_type AS refund_source_type, r.source_id AS refund_source_id, r.amount_paise AS refund_amount
FROM ledger_entries l
LEFT JOIN ledger_entries r ON r.refund_of = l.id AND r.entry_type = 'REFUND_CREDIT' AND r.status = 'SUCCESS'
WHERE l.merchant_id = ? AND l.entry_type = ? AND l.status = 'SUCCESS' AND l.direction = 'DEBIT'
"""


def rent_debits(conn, merchant_id: str, sub_id: str | None = None) -> list[dict]:
    sql = _DEBIT_SQL + (" AND l.source_type='subscription' AND l.source_id=?" if sub_id else "") + " ORDER BY l.value_date, l.id"
    return _rows(conn, sql, (merchant_id, "RENT_DEBIT") + ((sub_id,) if sub_id else ()))


def subscriptions(conn, merchant_id: str) -> list[dict]:
    return _rows(conn, """
        SELECT s.*, p.name AS plan_name, p.monthly_fee_paise AS fee, d.serial_no, d.status AS device_status,
               d.merchant_id AS device_merchant_id, d.model AS device_model
        FROM subscriptions s JOIN plans p ON p.id = s.plan_id LEFT JOIN devices d ON d.id = s.device_id
        WHERE s.merchant_id = ? ORDER BY s.started_at, s.id""", (merchant_id,))


def mandate_for(conn, *, subscription_id: str | None = None, loan_id: str | None = None) -> dict | None:
    if subscription_id:
        return _one(conn, "SELECT * FROM mandates WHERE subscription_id=? ORDER BY created_at DESC, id LIMIT 1", (subscription_id,))
    return _one(conn, "SELECT * FROM mandates WHERE loan_id=? ORDER BY created_at DESC, id LIMIT 1", (loan_id,))


def ledger_row(conn, ledger_id: int) -> dict | None:
    return _one(conn, "SELECT * FROM ledger_entries WHERE id=?", (ledger_id,))


def refunds_of(conn, ledger_ids: Iterable[int]) -> dict[int, dict]:
    ids = sorted({int(i) for i in ledger_ids})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = _rows(conn, f"SELECT * FROM ledger_entries WHERE entry_type='REFUND_CREDIT' AND status='SUCCESS' AND refund_of IN ({marks})", tuple(ids))
    return {r["refund_of"]: r for r in rows}


def debit_rows(conn, merchant_id: str, ledger_ids: Iterable[int]) -> list[dict]:
    ids = sorted({int(i) for i in ledger_ids})
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return _rows(conn, f"SELECT * FROM ledger_entries WHERE merchant_id=? AND direction='DEBIT' AND id IN ({marks}) ORDER BY value_date, id",
                 (merchant_id, *ids))


# ───────────────────────────── device lifecycle ─────────────────────────────

@dataclass
class Lifecycle:
    device_id: str
    events: list[dict]
    tickets: list[dict]
    request: dict | None = None            # latest RETURN_REQUESTED event
    pickup_at: str | None = None           # timestamp of PICKED_UP (or COMPLETED ticket)
    pickup_date: str | None = None         # YYYY-MM-DD
    pickup_ref: str | None = None          # AWB
    pickup_event: dict | None = None
    received: dict | None = None
    deactivated: dict | None = None
    failed: list[dict] = field(default_factory=list)   # PICKUP_FAILED after the request
    ticket: dict | None = None             # ticket for the request / pickup


def lifecycle(conn, device_id: str, merchant_id: str, since: str | None = None) -> Lifecycle:
    since = (since or "0000")[:10]
    events = _rows(conn, "SELECT * FROM device_events WHERE device_id=? AND at>=? ORDER BY at, id", (device_id, since))
    tickets = _rows(conn, "SELECT * FROM pickup_tickets WHERE device_id=? AND merchant_id=? AND requested_at>=? ORDER BY requested_at, id",
                    (device_id, merchant_id, since))
    lc = Lifecycle(device_id, events, tickets)
    reqs = [e for e in events if e["event_type"] == "RETURN_REQUESTED"]
    lc.request = reqs[-1] if reqs else None
    picked = [e for e in events if e["event_type"] == "PICKED_UP"]
    done = [t for t in tickets if t["status"] == "COMPLETED"]
    if picked:
        lc.pickup_event = picked[0]
        lc.pickup_at, lc.pickup_ref = picked[0]["at"], picked[0]["ref"]
    elif done:
        lc.pickup_at = done[0]["completed_at"] or done[0]["scheduled_for"] or done[0]["requested_at"]
        lc.pickup_ref = done[0]["courier_awb"]
    if lc.pickup_at:
        lc.pickup_date = lc.pickup_at[:10]
        lc.received = next((e for e in events if e["event_type"] == "RECEIVED_AT_WAREHOUSE" and e["at"] >= lc.pickup_at), None)
    lc.deactivated = next((e for e in events if e["event_type"] == "DEACTIVATED"), None)
    if lc.request:
        lc.failed = [e for e in events if e["event_type"] == "PICKUP_FAILED" and e["at"] >= lc.request["at"]]
        lc.ticket = next((t for t in tickets if t["id"] == lc.request["ref"]), None) or (tickets[-1] if tickets else None)
    elif done:
        lc.ticket = done[0]
    if lc.pickup_at and not lc.ticket:
        lc.ticket = done[0] if done else None
    return lc


def failed_phrase(lc: Lifecycle) -> str:
    """'06 Oct shop closed, 09 Oct merchant unavailable'"""
    return ", ".join(f"{fday(e['at'])} {human_code(e['note'] or e['ref'] or 'failed')}" for e in lc.failed)


@dataclass
class PaymentStats:
    count: int
    first: str | None
    last: str | None
    total: int
    last_id: int | None


def payments_via(conn, device_id: str | None, merchant_id: str, after: str | None = None, before: str | None = None) -> PaymentStats:
    cond, params = ["merchant_id=?", "status='SUCCESS'"], [merchant_id]
    if device_id:
        cond.append("device_id=?")
        params.append(device_id)
    if after:
        cond.append("paid_at>=?")
        params.append(after)
    if before:
        cond.append("paid_at<?")
        params.append(before)
    w = " AND ".join(cond)
    r = conn.execute(f"SELECT COUNT(*), MIN(paid_at), MAX(paid_at), COALESCE(SUM(amount_paise),0) FROM payments WHERE {w}", params).fetchone()
    last_id = conn.execute(f"SELECT id FROM payments WHERE {w} ORDER BY paid_at DESC, id DESC LIMIT 1", params).fetchone()
    return PaymentStats(r[0], r[1], r[2], r[3], last_id[0] if last_id else None)


# ───────────────────────────── failure-pattern analyses ─────────────────────────────

@dataclass
class ZombieSub:
    sub: dict
    lc: Lifecycle | None
    after: list[dict]                       # SUCCESS rent debits with value_date > pickup date (refunded or not)
    damages: list[dict]                     # ...not refunded


def zombie_analysis(conn, merchant_id: str) -> list[ZombieSub]:
    out = []
    for sub in subscriptions(conn, merchant_id):
        if not sub["device_id"]:
            continue
        lc = lifecycle(conn, sub["device_id"], merchant_id, sub["started_at"])
        after: list[dict] = []
        if lc.pickup_date:
            after = [d for d in rent_debits(conn, merchant_id, sub["id"]) if d["value_date"] > lc.pickup_date]
        out.append(ZombieSub(sub, lc, after, [d for d in after if not d["refund_id"]]))
    return out


@dataclass
class DupGroup:
    device_id: str
    original: dict
    later: list[dict]
    active: bool                            # ≥2 ACTIVE subscriptions right now (the catalogue's MATCH rule)
    extra: list[dict]                       # later subscriptions' SUCCESS debits while the original was live
    damages: list[dict]                     # ...not refunded, and only for ACTIVE later subscriptions


def duplicate_subscriptions(conn, merchant_id: str) -> list[DupGroup]:
    by_dev: dict[str, list[dict]] = {}
    for s in subscriptions(conn, merchant_id):
        if s["device_id"]:
            by_dev.setdefault(s["device_id"], []).append(s)
    out = []
    for dev, subs in by_dev.items():
        if len(subs) < 2:
            continue
        orig = subs[0]
        later = [s for s in subs[1:] if orig["ended_at"] is None or s["started_at"] < orig["ended_at"]]
        if not later:
            continue
        active_later = [s for s in later if s["status"] == "ACTIVE"] if orig["status"] == "ACTIVE" else []
        extra, damages = [], []
        for s in later:
            for d in rent_debits(conn, merchant_id, s["id"]):
                if orig["ended_at"] is None or d["value_date"] <= orig["ended_at"]:
                    extra.append(d)
                    if s in active_later and not d["refund_id"]:
                        damages.append(d)
        out.append(DupGroup(dev, orig, later, bool(active_later), extra, damages))
    return out


@dataclass
class LoanEdi:
    loan: dict
    by_due: dict[str, list[dict]]           # due date -> SUCCESS EDI debits (ordered posted_at, id) with attempt info
    dup_days: dict[str, list[dict]]         # due dates with >1 debit
    extra: list[dict]                       # all but the first debit on duplicate days (refunded or not)
    damages: list[dict]                     # ...not refunded


def edi_analysis(conn, merchant_id: str) -> list[LoanEdi]:
    out = []
    for loan in _rows(conn, "SELECT * FROM loans WHERE merchant_id=? ORDER BY disbursed_at, id", (merchant_id,)):
        rows = _rows(conn, _DEBIT_SQL.replace("SELECT l.*,", """SELECT l.*, a.id AS attempt_id, a.due_date, a.attempt_no,
                     a.status AS attempt_status, a.responded_at, a.gateway_ref,""").replace(
            "FROM ledger_entries l", "FROM ledger_entries l LEFT JOIN edi_attempts a ON a.ledger_entry_id = l.id") +
            " AND l.source_type='loan' AND l.source_id=? ORDER BY COALESCE(a.due_date, l.value_date), l.posted_at, l.id",
            (merchant_id, "EDI_DEBIT", loan["id"]))
        by_due: dict[str, list[dict]] = {}
        for r in rows:
            by_due.setdefault(r["due_date"] or r["value_date"], []).append(r)
        dup = {k: v for k, v in by_due.items() if len(v) > 1}
        extra = [d for v in dup.values() for d in v[1:]]
        out.append(LoanEdi(loan, by_due, dup, extra, [d for d in extra if not d["refund_id"]]))
    return out


def attempts_for(conn, loan_id: str, due: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM edi_attempts WHERE loan_id=? AND due_date=? ORDER BY attempt_no, id", (loan_id, due))


def wrongful_debits(conn, merchant_id: str, areas: set[str] | None = None, *, include_refunded: bool = True) -> dict[int, str]:
    """ledger id -> suspect id for every debit a FAULT pattern says should not have happened."""
    areas = set(AREAS) if areas is None else areas
    out: dict[int, str] = {}
    if RENT in areas:
        for z in zombie_analysis(conn, merchant_id):
            for d in (z.after if include_refunded else z.damages):
                out[d["id"]] = "ZOMBIE_BILLING"
        for g in duplicate_subscriptions(conn, merchant_id):
            for d in (g.extra if include_refunded else g.damages):
                out.setdefault(d["id"], "DUPLICATE_SUBSCRIPTION")
    if LOAN in areas:
        for le in edi_analysis(conn, merchant_id):
            for d in (le.extra if include_refunded else le.damages):
                out[d["id"]] = "DUPLICATE_EDI"
    return out


# ───────────────────────────── settlements, holds, KYC ─────────────────────────────

def settlements(conn, merchant_id: str, since: str | None = None) -> list[dict]:
    rows = _rows(conn, """
        SELECT s.*, COALESCE((SELECT SUM(p.mdr_paise) FROM payments p WHERE p.settlement_id = s.id), 0) AS mdr_paise,
               (SELECT COUNT(*) FROM payments p WHERE p.settlement_id = s.id) AS payment_count
        FROM settlements s WHERE s.merchant_id=? AND s.batch_date>=? ORDER BY s.batch_date, s.id""",
                 (merchant_id, since or "0000"))
    for r in rows:
        r["other_deductions"] = r["deductions_paise"] - r["mdr_paise"]
    return rows


def holds(conn, merchant_id: str, hold_type: str | None = None, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM holds WHERE merchant_id=?" + (" AND status='ACTIVE'" if active_only else "") + \
          (" AND hold_type=?" if hold_type else "") + " ORDER BY placed_at, id"
    return _rows(conn, sql, (merchant_id,) + ((hold_type,) if hold_type else ()))


def covered_settlements(conn, hold: dict) -> list[dict]:
    """Settlements a hold is keeping back: the linked batch, plus HELD batches on/after the day it was placed."""
    return [s for s in settlements(conn, hold["merchant_id"])
            if s["id"] == hold["settlement_id"] or (s["status"] == "HELD" and s["batch_date"] >= hold["placed_at"][:10])]


def holds_for_settlement(conn, stl: dict) -> list[dict]:
    hs = holds(conn, stl["merchant_id"], active_only=False)
    return [h for h in hs if h["settlement_id"] == stl["id"] or
            (stl["status"] == "HELD" and h["status"] == "ACTIVE" and stl["batch_date"] >= h["placed_at"][:10])]


def kyc_docs(conn, merchant_id: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM kyc_documents WHERE merchant_id=? ORDER BY id", (merchant_id,))


def doc_problem(doc: dict) -> str:
    label = DOC_LABEL.get(doc["doc_type"], human_code(doc["doc_type"]))
    if doc["status"] == "EXPIRED":
        return f"the {label} expired on {fdate(doc['expires_at'])}" if doc["expires_at"] else f"the {label} has expired"
    if doc["status"] == "PENDING":
        return f"the {label} is pending verification"
    if doc["status"] == "REJECTED":
        return f"the {label} was rejected"
    return ""


def stl_phrase(stls: list[dict]) -> str:
    """'Settlement STL-1005-20261111 (₹48,200 net, 11 Nov 2026)' or '3 settlements (11 – 13 Nov 2026, ₹31,640 net)'"""
    if len(stls) == 1:
        s = stls[0]
        return f"Settlement {s['id']} ({rupees(s['net_paise'])} net, {fdate(s['batch_date'])})"
    return f"{len(stls)} settlements ({fspan(stls[0]['batch_date'], stls[-1]['batch_date'])}, {rupees(sum(s['net_paise'] for s in stls))} net)"


# ───────────────────────────── evidence labels ─────────────────────────────

_ENTRY = {"RENT_DEBIT": "rent debit", "EDI_DEBIT": "EDI debit", "SETTLEMENT_CREDIT": "settlement credit",
          "REFUND_CREDIT": "refund credit", "CHARGEBACK_DEBIT": "chargeback debit"}


def ledger_label(r: dict) -> str:
    when = fdate(r["value_date"]) + (f" {ftime(r['posted_at'])}" if r["entry_type"] == "EDI_DEBIT" else "")
    return f"{rupees(r['amount_paise'])} {_ENTRY.get(r['entry_type'], human_code(r['entry_type']))} · {when} · {r['txn_ref']}"


def ledger_ref(r: dict) -> EvidenceRef:
    return EvidenceRef(table="ledger_entries", row_id=str(r["id"]), label=ledger_label(r), at=r["posted_at"],
                       amount_paise=r["amount_paise"])


def event_label(e: dict) -> str:
    extra = ""
    if e["event_type"] == "PICKUP_FAILED":
        extra = f" · {human_code(e['note'])}" if e.get("note") else ""
    elif e.get("ref"):
        extra = f" · {'AWB ' if str(e['ref']).startswith('DLV') else ''}{e['ref']}"
    return f"{EVENT_LABEL.get(e['event_type'], human_code(e['event_type']).capitalize())} · {fdate(e['at'])} {fhm(e['at'])}{extra}"


def event_ref(e: dict) -> EvidenceRef:
    return EvidenceRef(table="device_events", row_id=str(e["id"]), label=f"{e['device_id']} {event_label(e)}", at=e["at"])


def ticket_label(t: dict) -> str:
    bits = [f"Pickup {t['id']}", t["status"], plural(t["attempts"] or 0, "attempt")]
    if t.get("failure_reason") and t["status"] == "FAILED":
        bits.append(human_code(t["failure_reason"]))
    if t.get("courier_awb"):
        bits.append(f"AWB {t['courier_awb']}")
    return " · ".join(bits)


def ticket_ref(t: dict) -> EvidenceRef:
    return EvidenceRef(table="pickup_tickets", row_id=t["id"], label=ticket_label(t), at=t.get("completed_at") or t["requested_at"])


def sub_label(s: dict) -> str:
    end = f" · {s['status']} {fdate(s['ended_at'])}" if s.get("ended_at") else f" · {s['status']}"
    return f"{s['id']} · {s.get('plan_name') or s['plan_id']} {rupees(s.get('fee'))}/month · since {fdate(s['started_at'])}{end}"


def sub_ref(s: dict) -> EvidenceRef:
    return EvidenceRef(table="subscriptions", row_id=s["id"], label=sub_label(s), at=s["started_at"])


def hold_label(h: dict) -> str:
    kind = {"RISK": "Risk hold", "KYC": "KYC hold"}.get(h["hold_type"], f"{h['hold_type'].title()} hold")
    return f"{kind} {h['id']} · {h['reason_code']} · {h['status']} since {fdate(h['placed_at'])} {fhm(h['placed_at'])}".rstrip()


def hold_ref(h: dict) -> EvidenceRef:
    return EvidenceRef(table="holds", row_id=h["id"], label=hold_label(h), at=h["placed_at"])


def stl_label(s: dict) -> str:
    tail = f" · UTR {s['utr']}" if s.get("utr") else ""
    return (f"{s['id']} · {fdate(s['batch_date'])} · {s['status']} · net {rupees(s['net_paise'])} "
            f"(gross {rupees(s['gross_paise'])} − {rupees(s['deductions_paise'])}){tail}")


def stl_ref(s: dict) -> EvidenceRef:
    return EvidenceRef(table="settlements", row_id=s["id"], label=stl_label(s), at=s.get("settled_at") or s["batch_date"],
                       amount_paise=s["net_paise"])


def kyc_label(d: dict) -> str:
    label = DOC_LABEL.get(d["doc_type"], human_code(d["doc_type"]))
    label = label[:1].upper() + label[1:]
    when = f" {fdate(d['expires_at'])}" if d["status"] == "EXPIRED" and d.get("expires_at") else ""
    return f"{label} · {d['status']}{when}"


def kyc_ref(d: dict) -> EvidenceRef:
    return EvidenceRef(table="kyc_documents", row_id=str(d["id"]), label=kyc_label(d), at=d.get("expires_at") or d.get("verified_at"))


def payments_ref(stats: PaymentStats, label: str) -> EvidenceRef:
    return EvidenceRef(table="payments", row_id=str(stats.last_id or ""), label=label, at=stats.last, amount_paise=stats.total)


def attempt_label(a: dict) -> str:
    return f"Attempt {a['attempt_no']} · {a['status']} at {ftime(a['responded_at'])} · {a['gateway_ref']}"


def attempt_ref(a: dict) -> EvidenceRef:
    return EvidenceRef(table="edi_attempts", row_id=str(a["id"]), label=f"{a['loan_id']} {fday(a['due_date'])} {attempt_label(a)}",
                       at=a["responded_at"])
