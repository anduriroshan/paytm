"""Deterministic demo data: the scenario merchants (full histories), ~190 background merchants and the travel passengers.

    python -m munim.db.seed            # recreate backend/data/munim.db

Everything is fictional (businesses, owners, lenders) and phones are masked. `random.Random(42)` drives
all variation, so two runs produce byte-identical business data. Scenario facts (ids, dates, amounts)
come from docs/CONTRACTS.md and are written explicitly, never sampled.
"""
from __future__ import annotations

import gc
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from munim import config
from munim.db.conn import connect, create_schema

TODAY = date(2026, 11, 14)            # demo "today" (config.DEMO_TODAY); business data runs up to the day before
LAST_DAY = TODAY - timedelta(days=1)
SEED = 42

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

COLUMNS: dict[str, tuple[str, ...]] = {   # insertion order respects foreign keys
    "merchants": ("id", "business_name", "owner_name", "phone", "city", "language", "category", "onboarded_at", "status"),
    "plans": ("id", "name", "device_model", "monthly_fee_paise"),
    "devices": ("id", "serial_no", "model", "merchant_id", "status", "installed_at"),
    "device_events": ("id", "device_id", "event_type", "at", "ref", "note"),
    "pickup_tickets": ("id", "device_id", "merchant_id", "status", "requested_at", "scheduled_for", "completed_at",
                       "courier_awb", "attempts", "failure_reason"),
    "subscriptions": ("id", "merchant_id", "device_id", "plan_id", "status", "started_at", "ended_at", "end_reason"),
    "loans": ("id", "merchant_id", "lender", "principal_paise", "edi_amount_paise", "tenure_days", "status", "disbursed_at"),
    "mandates": ("id", "merchant_id", "purpose", "subscription_id", "loan_id", "umrn", "max_amount_paise", "status", "created_at"),
    "settlements": ("id", "merchant_id", "batch_date", "gross_paise", "deductions_paise", "net_paise", "status", "utr", "settled_at"),
    "payments": ("id", "merchant_id", "device_id", "mode", "amount_paise", "mdr_paise", "status", "paid_at", "settlement_id"),
    "chargebacks": ("id", "merchant_id", "payment_id", "amount_paise", "reason", "status", "raised_at", "debited_at"),
    "holds": ("id", "merchant_id", "settlement_id", "hold_type", "reason_code", "reason_text", "status", "placed_at", "released_at"),
    "kyc_documents": ("id", "merchant_id", "doc_type", "status", "verified_at", "expires_at"),
    "ledger_entries": ("id", "merchant_id", "txn_ref", "entry_type", "direction", "amount_paise", "status", "source_type",
                       "source_id", "mandate_id", "value_date", "posted_at", "narration", "refund_of"),
    "edi_attempts": ("id", "loan_id", "due_date", "attempt_no", "gateway_ref", "status", "responded_at", "ledger_entry_id"),
    "operators": ("id", "name", "kind"),
    "trips": ("id", "operator_id", "kind", "service_no", "origin", "destination", "departs_at", "arrives_at", "status", "status_at"),
    "bookings": ("id", "passenger_id", "trip_id", "passenger_name", "seat", "pnr", "pax", "fare_paise", "status", "booked_at",
                 "boarded_at", "cancelled_at", "ledger_entry_id"),
    "trip_events": ("id", "trip_id", "source", "event_type", "at", "km", "note"),
    "airline_refunds": ("id", "operator_id", "pnr", "passenger_name", "amount_paise", "received_at", "booking_id", "status"),
    "support_calls": ("id", "merchant_id", "booking_id", "at", "summary"),
}
WORKFLOW_TABLES = ("cases", "sweeps", "actions", "notifications", "audit_log")

MODEL_4G, MODEL_LITE = "Soundbox 4G", "Soundbox Lite"
PLANS = [("PLAN_SB499", "Soundbox 4G Rental", MODEL_4G, 49900), ("PLAN_SB299", "Soundbox Lite", MODEL_LITE, 29900)]
PLAN_NAME = {p[0]: p[1] for p in PLANS}
PLAN_MODEL = {p[0]: p[2] for p in PLANS}
PLAN_FEE = {p[0]: p[3] for p in PLANS}

# ───────────────────────────── fictional population ─────────────────────────────

REGIONS = {  # name: (weight, [(city, language)], first names, last names)
    "north": (34, [("Lucknow", "hi"), ("Kanpur", "hi"), ("Varanasi", "hi"), ("Jaipur", "hi"), ("Bhopal", "hi"),
                   ("Indore", "hi"), ("Patna", "hi"), ("Agra", "hi"), ("Dehradun", "hi"), ("Delhi", "hi-Latn"),
                   ("Gurugram", "hi-Latn"), ("Noida", "hi-Latn")],
              ["Ramesh", "Suresh", "Rajesh", "Anil", "Sunil", "Vinod", "Manoj", "Pankaj", "Sanjay", "Deepak", "Amit",
               "Rakesh", "Mukesh", "Ashok", "Neha", "Pooja", "Sunita", "Kavita", "Rekha", "Anita", "Ritu", "Saurabh",
               "Gaurav", "Vikas", "Alok", "Naveen"],
              ["Sharma", "Verma", "Gupta", "Agarwal", "Yadav", "Singh", "Mishra", "Tiwari", "Pandey", "Srivastava",
               "Jain", "Saxena", "Chauhan", "Rastogi", "Bansal", "Kushwaha"]),
    "te": (15, [("Hyderabad", "te"), ("Vijayawada", "te"), ("Visakhapatnam", "te"), ("Warangal", "te"),
                ("Guntur", "te"), ("Tirupati", "te"), ("Nellore", "te")],
           ["Venkata", "Srinivas", "Ravi", "Prasad", "Lakshmi", "Padma", "Ramana", "Naresh", "Kiran", "Sravani",
            "Anusha", "Madhavi", "Nagaraju", "Sai Kumar"],
           ["Reddy", "Rao", "Naidu", "Chowdary", "Varma", "Goud", "Raju", "Murthy", "Sastry"]),
    "ta": (9, [("Chennai", "ta"), ("Coimbatore", "ta"), ("Madurai", "ta"), ("Tiruchirappalli", "ta"), ("Salem", "ta")],
           ["Murugan", "Senthil", "Karthik", "Meena", "Saravanan", "Selvi", "Arun", "Bala", "Kavitha", "Ganesan"],
           ["Subramanian", "Pillai", "Raman", "Krishnan", "Natarajan", "Sundaram", "Iyer", "Chettiar"]),
    "mr": (12, [("Pune", "mr"), ("Mumbai", "hi-Latn"), ("Nagpur", "mr"), ("Nashik", "mr"), ("Kolhapur", "mr"),
                ("Aurangabad", "mr")],
           ["Sachin", "Mahesh", "Ganesh", "Santosh", "Prakash", "Swati", "Snehal", "Rohit", "Amol", "Vaishali"],
           ["Patil", "Kulkarni", "Deshmukh", "Jadhav", "Shinde", "Pawar", "Joshi", "Gaikwad", "More"]),
    "gu": (10, [("Ahmedabad", "gu"), ("Surat", "gu"), ("Vadodara", "gu"), ("Rajkot", "gu")],
           ["Hitesh", "Jignesh", "Bhavesh", "Nilesh", "Kalpesh", "Hetal", "Mehul", "Dharmesh", "Komal"],
           ["Patel", "Shah", "Mehta", "Desai", "Parmar", "Trivedi", "Solanki"]),
    "kn": (9, [("Bengaluru", "en"), ("Bengaluru", "kn"), ("Mysuru", "kn"), ("Hubballi", "kn"), ("Mangaluru", "kn")],
           ["Manjunath", "Basavaraj", "Raghavendra", "Shivakumar", "Nagesh", "Deepa", "Shilpa", "Harish"],
           ["Gowda", "Shetty", "Hegde", "Kamath", "Naik", "Bhat"]),
    "bn": (7, [("Kolkata", "bn"), ("Howrah", "bn"), ("Siliguri", "bn")],
           ["Subhash", "Partha", "Debashis", "Sourav", "Moumita", "Rupa", "Arindam", "Tapas"],
           ["Das", "Ghosh", "Banerjee", "Chatterjee", "Mukherjee", "Sen", "Roy", "Bose"]),
    "ml": (4, [("Kochi", "ml"), ("Thiruvananthapuram", "ml"), ("Kozhikode", "ml")],
           ["Biju", "Shaji", "Lekha", "Sreeja", "Joseph", "Anoop", "Rajeev"],
           ["Nair", "Menon", "Kurian", "Varghese", "Thomas", "Panicker"]),
}

CATEGORIES = {  # category: (weight, shop-name suffixes, ticket range in rupees, card share)
    "kirana": (24, ["Kirana Store", "General Store", "Provision Store", "Departmental Store", "Super Bazaar"], (20, 900), 0.02),
    "pharmacy": (10, ["Medicals", "Medical Store", "Pharmacy", "Chemists"], (40, 1500), 0.05),
    "restaurant": (12, ["Tiffins", "Bhojanalaya", "Dhaba", "Family Restaurant", "Mess", "Biryani House"], (60, 900), 0.05),
    "electronics": (7, ["Electronics", "Mobile Point", "Mobile Centre", "Digital World"], (150, 6000), 0.15),
    "sweets": (6, ["Sweets", "Mithai Bhandar", "Sweet House"], (40, 1200), 0.04),
    "apparel": (8, ["Garments", "Textiles", "Fashion", "Saree Centre"], (200, 3500), 0.12),
    "hardware": (6, ["Hardware", "Paints & Hardware", "Traders"], (100, 4000), 0.08),
    "salon": (5, ["Hair Salon", "Beauty Parlour", "Men's Saloon"], (60, 1200), 0.04),
    "stationery": (5, ["Stationers", "Book Depot", "Xerox & Stationery"], (10, 600), 0.02),
    "bakery": (6, ["Bakery", "Bakers", "Cake Shop"], (30, 900), 0.03),
    "fruits_vegetables": (6, ["Fruits & Vegetables", "Sabzi Bhandar", "Fresh Fruits"], (20, 500), 0.0),
    "tea_stall": (5, ["Tea Stall", "Chai Point", "Tea House"], (10, 150), 0.0),
}
DEITY = ["Shree Balaji", "Om Sai", "Maa Durga", "Jai Ambe", "Sri Ganesh", "Shiv Shakti", "Annapurna", "Sri Venkateswara",
         "Jai Hanuman", "Gayatri", "Mahalaxmi", "Sri Krishna", "Shree Ram", "Sai Ram", "Jai Mata Di"]
ADJ = ["New", "Royal", "Super", "City", "Modern", "Famous", "Golden", "Lucky", "Prime", "Classic", "Star", "Fresh",
       "Janta", "Apna", "Sunrise", "Galaxy"]
LENDERS = ["Arthveda Finance", "Kanakdhara Credit", "Vrddhi Fincap", "Sambal Lending"]   # fictional NBFC partners
RISK_REASONS = [
    ("VELOCITY_SPIKE", "Card volume 9x the 30-day average within 2 hours"),
    ("UNUSUAL_TICKET_SIZE", "Single UPI collection far above the merchant's average ticket size"),
    ("CHARGEBACK_RATIO", "Chargeback ratio 3.1% over 30 days, above the 1% threshold"),
]
FAIL_REASONS = ["SHOP_CLOSED", "MERCHANT_UNAVAILABLE", "ADDRESS_NOT_FOUND", "MERCHANT_REFUSED"]


def _ts(d: date, h: int, m: int, s: int = 0) -> str:
    return f"{d.isoformat()}T{h:02d}:{m:02d}:{s:02d}"


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _firsts(after: date, until: date) -> list[date]:
    """First-of-month dates strictly after `after` and on/before `until` (monthly rent debit dates)."""
    out, y, m = [], after.year, after.month
    while True:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        d = date(y, m, 1)
        if d > until:
            return out
        out.append(d)


class _Seeder:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.t: dict[str, list[tuple]] = {k: [] for k in COLUMNS}
        self.pay: list[list] = []                    # payments stay mutable until settlements are assigned
        self.seq: dict[str, int] = defaultdict(int)
        self.serials: set[str] = set()
        self.names: set[str] = set()

    def next(self, key: str) -> int:
        self.seq[key] += 1
        return self.seq[key]

    # ── rows ──
    def phone(self) -> str:
        return f"{self.rng.choice(['98', '97', '96', '95', '94', '93', '91', '90', '89', '88', '87', '86', '85', '84', '83', '81', '80', '79', '78', '77', '76', '73', '70'])}XXXX{self.rng.randint(1000, 9999)}"

    def merchant(self, mid, name, owner, city, lang, cat, onboarded, phone=None):
        self.names.add(name)
        self.t["merchants"].append((mid, name, owner, phone or self.phone(), city, lang, cat, str(onboarded), "ACTIVE"))

    def device(self, did, serial, model, mid, status, installed):
        self.serials.add(serial)
        self.t["devices"].append((did, serial, model, mid, status, str(installed)))

    def serial(self, model: str) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            s = ("SB4G-" if model == MODEL_4G else "SBLT-") + "".join(self.rng.choice(alphabet) for _ in range(6))
            if s not in self.serials:
                return s

    def event(self, did, etype, at, ref=None, note=None) -> int:
        eid = self.next("event")
        self.t["device_events"].append((eid, did, etype, at, ref, note))
        return eid

    def ticket(self, tid, did, mid, status, requested, scheduled, completed, awb, attempts, failure):
        self.t["pickup_tickets"].append((tid, did, mid, status, requested, scheduled, completed, awb, attempts, failure))

    def subscription(self, sid, mid, did, plan, status, started, ended=None, reason=None):
        self.t["subscriptions"].append((sid, mid, did, plan, status, str(started), ended and str(ended), reason))

    def mandate(self, mdt, mid, purpose, *, sub=None, loan=None, max_paise, status="ACTIVE", created):
        umrn = f"PYTM{str(created).replace('-', '')[2:8]}{self.next('umrn'):08d}"
        self.t["mandates"].append((mdt, mid, purpose, sub, loan, umrn, max_paise, status, str(created)))

    def loan(self, lid, mid, lender, principal, edi, tenure, disbursed, status="ACTIVE"):
        self.t["loans"].append((lid, mid, lender, principal, edi, tenure, status, str(disbursed)))

    def ledger(self, mid, etype, amount, value_date, posted_at, source_type, source_id, narration, *,
               mandate_id=None, status="SUCCESS", refund_of=None) -> int:
        lid = self.next("ledger")
        direction = "CREDIT" if etype in ("SETTLEMENT_CREDIT", "REFUND_CREDIT") else "DEBIT"
        prefix = {"RENT_DEBIT": "NACH", "EDI_DEBIT": "NACH", "SETTLEMENT_CREDIT": "NEFT",
                  "REFUND_CREDIT": "RFD", "CHARGEBACK_DEBIT": "CBK", "BOOKING_DEBIT": "UPI"}[etype]
        txn_ref = f"{prefix}{str(value_date).replace('-', '')[2:]}{lid:07d}"
        self.t["ledger_entries"].append((lid, mid, txn_ref, etype, direction, amount, status, source_type, source_id,
                                         mandate_id, str(value_date), posted_at, narration, refund_of))
        return lid

    def kyc(self, mid, doc, status="VALID", verified=None, expires=None):
        self.t["kyc_documents"].append((self.next("kyc"), mid, doc, status, verified and str(verified), expires and str(expires)))

    def hold(self, hid, mid, stl, htype, code, text, placed, status="ACTIVE"):
        self.t["holds"].append((hid, mid, stl, htype, code, text, status, placed, None))

    def payment(self, mid, did, mode, amount, mdr, paid_at, status="SUCCESS") -> int:
        pid = self.next("payment")
        self.pay.append([pid, mid, did, mode, amount, mdr, status, paid_at, None])
        return pid

    # ── series ──
    def rent(self, mid, sub_id, mandate_id, plan, dates):
        for d in dates:
            posted = _ts(d, 6, self.rng.randint(2, 58), self.rng.randint(0, 59))
            self.ledger(mid, "RENT_DEBIT", PLAN_FEE[plan], d, posted, "subscription", sub_id,
                        f"{PLAN_NAME[plan]} rent {MONTHS[d.month - 1]} {d.year} · {sub_id}", mandate_id=mandate_id)

    def _attempt(self, loan_id, due, no, status, responded, ledger_id):
        gw = f"GW{due.strftime('%Y%m%d')}{self.next('gw'):06d}"
        self.t["edi_attempts"].append((self.next("edi"), loan_id, str(due), no, gw, status, responded, ledger_id))

    def edi(self, mid, loan_id, mandate_id, lender, amount, dates, *, dup_dates=(), failures=False,
            exact_dup_times=False):
        for d in dates:
            narr = f"Loan EDI {loan_id} · {lender} · due {d.day:02d} {MONTHS[d.month - 1]}"
            if d in dup_dates:
                # Gateway timed out on attempt 1, but the bank had debited anyway; the 06:15 retry debited again.
                if exact_dup_times:
                    r1, p1, r2, p2 = _ts(d, 6, 0, 30), _ts(d, 6, 0, 41), _ts(d, 6, 15, 4), _ts(d, 6, 15, 9)
                else:
                    s = self.rng.randint(20, 40)
                    r1, p1 = _ts(d, 6, 0, s), _ts(d, 6, 0, s + self.rng.randint(5, 15))
                    r2, p2 = _ts(d, 6, 15, self.rng.randint(1, 20)), _ts(d, 6, 15, self.rng.randint(25, 50))
                l1 = self.ledger(mid, "EDI_DEBIT", amount, d, p1, "loan", loan_id, narr, mandate_id=mandate_id)
                self._attempt(loan_id, d, 1, "TIMEOUT", r1, l1)
                l2 = self.ledger(mid, "EDI_DEBIT", amount, d, p2, "loan", loan_id, narr, mandate_id=mandate_id)
                self._attempt(loan_id, d, 2, "SUCCESS", r2, l2)
            elif failures and self.rng.random() < 0.03:
                self._attempt(loan_id, d, 1, "FAILED", _ts(d, 6, 0, self.rng.randint(5, 50)), None)
                p = _ts(d, 18, 0, self.rng.randint(10, 50))
                lid = self.ledger(mid, "EDI_DEBIT", amount, d, p, "loan", loan_id, narr, mandate_id=mandate_id)
                self._attempt(loan_id, d, 2, "SUCCESS", p, lid)
            else:
                s = self.rng.randint(3, 40)
                lid = self.ledger(mid, "EDI_DEBIT", amount, d, _ts(d, 6, 0, s + 2), "loan", loan_id, narr,
                                  mandate_id=mandate_id)
                self._attempt(loan_id, d, 1, "SUCCESS", _ts(d, 6, 0, s), lid)

    def amount(self, lo: int, hi: int) -> int:
        """Rupee ticket skewed towards small values, returned in paise."""
        return int(round(lo + (hi - lo) * (self.rng.random() ** 2.2))) * 100

    def times(self, n: int, start_h: int = 8, end_h: int = 22) -> list[tuple[int, int, int]]:
        ts = sorted((self.rng.randint(start_h, end_h - 1), self.rng.randint(0, 59), self.rng.randint(0, 59)) for _ in range(n))
        return ts

    def day_payments(self, mid, d, did, n, ticket, card_share=0.0, fail_p=0.0):
        out = []
        for h, m, s in self.times(n):
            card = self.rng.random() < card_share
            amt = self.amount(ticket[0], ticket[1] * (2 if card else 1))
            mdr = round(amt * 0.012) if card else 0
            status = "FAILED" if self.rng.random() < fail_p else "SUCCESS"
            out.append(self.payment(mid, did, "CARD" if card else "UPI", amt, mdr, _ts(d, h, m, s), status))
        return out

    def split(self, total_paise: int, n: int, lo: int = 20) -> list[int]:
        rupees_total = total_paise // 100
        w = [self.rng.uniform(0.4, 1.6) for _ in range(n)]
        parts = [max(lo, int(rupees_total * x / sum(w))) for x in w[:-1]]
        last = rupees_total - sum(parts)
        assert last >= lo, (total_paise, n)
        return [p * 100 for p in parts] + [last * 100]

    def settle(self, mid, *, held: set[str] = frozenset(), extra: dict[str, int] | None = None, since: str | None = None):
        """Daily T+0 settlement batches from the merchant's SUCCESS payments (gross − MDR − extra = net)."""
        extra = extra or {}
        by_day: dict[str, list[list]] = defaultdict(list)
        for p in self.pay:
            if p[1] == mid and p[6] == "SUCCESS" and p[8] is None and (since is None or p[7][:10] >= since):
                by_day[p[7][:10]].append(p)
        for day in sorted(by_day):
            ps = by_day[day]
            sid = f"STL-{mid[1:]}-{day.replace('-', '')}"
            gross = sum(p[4] for p in ps)
            deductions = sum(p[5] for p in ps) + extra.get(day, 0)
            net = gross - deductions
            for p in ps:
                p[8] = sid
            if day in held:
                self.t["settlements"].append((sid, mid, day, gross, deductions, net, "HELD", None, None))
                continue
            d = date.fromisoformat(day)
            utr = f"PYTMN{day.replace('-', '')[2:]}{self.next('utr'):07d}"
            settled_at = _ts(d, 21, self.rng.randint(15, 45), self.rng.randint(0, 59))
            self.t["settlements"].append((sid, mid, day, gross, deductions, net, "SETTLED", utr, settled_at))
            if net > 0:
                self.ledger(mid, "SETTLEMENT_CREDIT", net, d, settled_at, "settlement", sid,
                            f"Settlement {sid} · {len(ps)} payments · UTR {utr}")

    def business_name(self, cat: str, last: str, city: str) -> str:
        suffixes = CATEGORIES[cat][1]
        for _ in range(12):
            r = self.rng.random()
            head = last if r < 0.4 else (self.rng.choice(DEITY) if r < 0.75 else self.rng.choice(ADJ))
            name = f"{head} {self.rng.choice(suffixes)}"
            if name not in self.names:
                return name
        return f"{head} {self.rng.choice(suffixes)} {city}"


# ───────────────────────────── the 8 scenario merchants ─────────────────────────────

def _base_rental(s: _Seeder, mid: str, did: str, serial: str, plan: str, installed: str, *, device_status="ACTIVE",
                 device_merchant: str | None = "same", shop: str = ""):
    """Device + INSTALLED event + subscription -A + mandate -A + monthly rent up to 1 Nov 2026."""
    n = mid[1:]
    inst = date.fromisoformat(installed)
    s.device(did, serial, PLAN_MODEL[plan], mid if device_merchant == "same" else device_merchant, device_status, installed)
    s.event(did, "INSTALLED", _ts(inst, 12, 30), f"INS-{inst.strftime('%y%m%d')}-{n}", f"Installed at {shop}".strip())
    s.subscription(f"SUB-{n}-A", mid, did, plan, "ACTIVE", installed)
    s.mandate(f"MDT-{n}-A", mid, "SUBSCRIPTION", sub=f"SUB-{n}-A", max_paise=PLAN_FEE[plan] * 2, created=installed)
    s.rent(mid, f"SUB-{n}-A", f"MDT-{n}-A", plan, _firsts(inst, LAST_DAY))


def _daily(s: _Seeder, mid, start: date, end: date, did, per_day, ticket, card_share=0.0, skip=()):
    for d in _days(start, end):
        if d not in skip:
            s.day_payments(mid, d, did, s.rng.randint(*per_day), ticket, card_share)


def _seed_scenarios(s: _Seeder) -> None:
    rng = s.rng
    win = TODAY - timedelta(days=30)                     # 15 Oct 2026: start of the 30-day payment window

    # S1 zombie_rent ─ M1001 Sharma Kirana Store
    s.merchant("M1001", "Sharma Kirana Store", "Ramesh Sharma", "Lucknow", "hi", "kirana", "2025-11-18", "94XXXX3187")
    _base_rental(s, "M1001", "D50001", "SB4G-7Q2K91", "PLAN_SB499", "2025-11-20", device_status="RETURNED",
                 device_merchant=None, shop="Sharma Kirana Store")
    s.event("D50001", "RETURN_REQUESTED", "2026-09-02T11:24:00", "PK-260902-1001", "Merchant requested return in app")
    s.event("D50001", "PICKUP_SCHEDULED", "2026-09-05T10:02:00", "PK-260902-1001", "Pickup slot 12 Sep 2026")
    s.event("D50001", "PICKED_UP", "2026-09-12T15:40:00", "DLV88123401", "Collected by courier from the shop")
    s.event("D50001", "RECEIVED_AT_WAREHOUSE", "2026-09-16T12:10:00", "DLV88123401", "Received at Lucknow warehouse; QC passed")
    s.ticket("PK-260902-1001", "D50001", "M1001", "COMPLETED", "2026-09-02T11:24:00", "2026-09-12",
             "2026-09-12T15:40:00", "DLV88123401", 1, None)
    _daily(s, "M1001", date(2026, 8, 15), date(2026, 9, 11), "D50001", (2, 5), (20, 900))
    _daily(s, "M1001", date(2026, 9, 12), LAST_DAY, None, (2, 4), (20, 900))        # static QR after the return
    s.settle("M1001")
    s.kyc("M1001", "PAN", verified="2025-11-18")
    s.kyc("M1001", "AADHAAR", verified="2025-11-18")

    # S2 double_billing ─ M1002 Lakshmi Tiffins: one device, two ACTIVE subscriptions
    s.merchant("M1002", "Lakshmi Tiffins", "K. Lakshmi", "Hyderabad", "te", "restaurant", "2026-03-12", "90XXXX4471")
    _base_rental(s, "M1002", "D50002", "SB4G-3M8T55", "PLAN_SB499", "2026-03-15", shop="Lakshmi Tiffins")
    s.subscription("SUB-1002-B", "M1002", "D50002", "PLAN_SB499", "ACTIVE", "2026-06-20")   # duplicate re-onboarding
    s.mandate("MDT-1002-B", "M1002", "SUBSCRIPTION", sub="SUB-1002-B", max_paise=99800, created="2026-06-20")
    s.rent("M1002", "SUB-1002-B", "MDT-1002-B", "PLAN_SB499", _firsts(date(2026, 6, 20), LAST_DAY))
    _daily(s, "M1002", win, LAST_DAY, "D50002", (4, 9), (60, 600))
    s.settle("M1002")
    for doc in ("PAN", "AADHAAR"):
        s.kyc("M1002", doc, verified="2026-03-12")
    s.kyc("M1002", "SHOP_LICENSE", verified="2026-03-12", expires="2028-03-31")

    # S3 duplicate_edi ─ M1003 Patel Mobile Point
    s.merchant("M1003", "Patel Mobile Point", "Hitesh Patel", "Ahmedabad", "hi-Latn", "electronics", "2025-06-02", "98XXXX6620")
    _base_rental(s, "M1003", "D50003", "SB4G-5H2W17", "PLAN_SB499", "2025-06-05", shop="Patel Mobile Point")
    s.loan("LN-1003", "M1003", "Arthveda Finance", 15000000, 125000, 150, "2026-09-01")
    s.mandate("MDT-1003-L", "M1003", "LOAN_EDI", loan="LN-1003", max_paise=250000, created="2026-09-01")
    s.edi("M1003", "LN-1003", "MDT-1003-L", "Arthveda Finance", 125000, list(_days(date(2026, 9, 2), LAST_DAY)),
          dup_dates={date(2026, 11, 12)}, exact_dup_times=True)
    _daily(s, "M1003", win, LAST_DAY, "D50003", (2, 6), (150, 4000), card_share=0.1)
    s.settle("M1003")
    for doc in ("PAN", "AADHAAR", "GSTIN"):
        s.kyc("M1003", doc, verified="2025-06-02")

    # S4 legit_rent ─ M1004 Gupta General Store: return requested, pickup failed twice, device still in use
    s.merchant("M1004", "Gupta General Store", "Suresh Gupta", "Jaipur", "hi", "kirana", "2025-08-08", "97XXXX2054")
    _base_rental(s, "M1004", "D50004", "SB4G-9P4D20", "PLAN_SB499", "2025-08-10", device_status="RETURN_REQUESTED",
                 shop="Gupta General Store")
    s.event("D50004", "RETURN_REQUESTED", "2026-10-03T10:15:00", "PK-261003-1004", "Merchant requested return in app")
    s.event("D50004", "PICKUP_SCHEDULED", "2026-10-04T09:30:00", "PK-261003-1004", "Pickup slot 06 Oct 2026")
    s.event("D50004", "PICKUP_FAILED", "2026-10-06T13:20:00", "PK-261003-1004", "SHOP_CLOSED")
    s.event("D50004", "PICKUP_FAILED", "2026-10-09T16:45:00", "PK-261003-1004", "MERCHANT_UNAVAILABLE")
    s.ticket("PK-261003-1004", "D50004", "M1004", "FAILED", "2026-10-03T10:15:00", "2026-10-09", None,
             "DLV88456702", 2, "MERCHANT_UNAVAILABLE")
    _daily(s, "M1004", date(2026, 8, 15), date(2026, 10, 2), "D50004", (1, 3), (20, 900))
    days = list(_days(date(2026, 10, 10), LAST_DAY))           # shop shut 03–09 Oct, then 41 payments via D50004
    counts = {d: 1 for d in days}
    for d in rng.sample(days[1:-1], 41 - len(days)):
        counts[d] += 1
    for d in days:
        s.day_payments("M1004", d, "D50004", counts[d], (20, 900))
    s.settle("M1004")
    s.kyc("M1004", "PAN", verified="2025-08-08")
    s.kyc("M1004", "AADHAAR", verified="2025-08-08")

    # S5 risk_hold ─ M1005 Royal Electronics: 11 Nov settlement held for a card velocity spike
    s.merchant("M1005", "Royal Electronics", "Mohammed Irfan", "Hyderabad", "en", "electronics", "2025-04-20", "99XXXX8812")
    _base_rental(s, "M1005", "D50005", "SB4G-2R6N48", "PLAN_SB499", "2025-04-22", shop="Royal Electronics")
    spike = date(2026, 11, 11)
    _daily(s, "M1005", win, LAST_DAY, "D50005", (4, 8), (200, 3000), card_share=0.04, skip={spike})
    cards = [685000, 662000, 699000, 640000, 674000, 660000]                     # ₹40,200 in 38 minutes
    mdr = [round(c * 40000 / sum(cards)) for c in cards[:-1]]
    mdr.append(40000 - sum(mdr))                                                 # MDR exactly ₹400
    upi = s.split(4860000 - sum(cards), 9, lo=200)
    upi_t = sorted((rng.randint(10, 18), rng.randint(0, 59), rng.randint(0, 59)) for _ in upi)
    for amt, (h, m, sec) in zip(upi, upi_t):
        s.payment("M1005", "D50005", "UPI", amt, 0, _ts(spike, h, m, sec))
    for amt, fee, (h, m) in zip(cards, mdr, [(19, 42), (19, 49), (19, 55), (20, 3), (20, 11), (20, 20)]):
        s.payment("M1005", "D50005", "CARD", amt, fee, _ts(spike, h, m, rng.randint(0, 59)))
    s.settle("M1005", held={"2026-11-11"})
    s.hold("HLD-1005-01", "M1005", "STL-1005-20261111", "RISK", "VELOCITY_SPIKE",
           "Card volume 14x the 30-day average; 6 high-value card payments from one BIN within 40 minutes",
           "2026-11-11T21:10:00")
    for doc in ("PAN", "AADHAAR", "GSTIN"):
        s.kyc("M1005", doc, verified="2025-04-20")

    # S6 kyc_hold ─ M1006 Sri Sai Medicals: drug licence expired, three settlements held
    s.merchant("M1006", "Sri Sai Medicals", "P. Venkata Rao", "Vijayawada", "te", "pharmacy", "2025-10-03", "96XXXX3349")
    _base_rental(s, "M1006", "D50006", "SB4G-8K1V63", "PLAN_SB499", "2025-10-06", shop="Sri Sai Medicals")
    _daily(s, "M1006", win, date(2026, 11, 10), "D50006", (6, 12), (40, 1500))
    for d, total, n in ((date(2026, 11, 11), 980000, 10), (date(2026, 11, 12), 1124000, 12), (date(2026, 11, 13), 1060000, 11)):
        for amt, (h, m, sec) in zip(s.split(total, n, lo=40), s.times(n)):
            s.payment("M1006", "D50006", "UPI", amt, 0, _ts(d, h, m, sec))
    s.settle("M1006", held={"2026-11-11", "2026-11-12", "2026-11-13"})
    s.kyc("M1006", "PAN", verified="2025-10-04")
    s.kyc("M1006", "GSTIN", verified="2025-10-04")
    s.kyc("M1006", "DRUG_LICENSE", "EXPIRED", verified="2025-10-04", expires="2026-11-10")
    s.hold("HLD-1006-01", "M1006", "STL-1006-20261111", "KYC", "KYC_DOC_EXPIRED", "Drug licence expired on 10 Nov 2026",
           "2026-11-11T06:00:00")

    # S7 prompt_injection ─ M1007 QuickMart Express: healthy history, nothing anomalous
    s.merchant("M1007", "QuickMart Express", "Arjun Shetty", "Bengaluru", "en", "kirana", "2025-02-14", "80XXXX5126")
    _base_rental(s, "M1007", "D50007", "SBLT-4C9X22", "PLAN_SB299", "2025-02-16", shop="QuickMart Express")
    _daily(s, "M1007", win, LAST_DAY, "D50007", (3, 7), (20, 900))
    s.settle("M1007")
    s.kyc("M1007", "PAN", verified="2025-02-14")
    s.kyc("M1007", "AADHAAR", verified="2025-02-14")

    # S8 cold_case ─ M1008 Krishna Sweets: 13 Nov settlement short by a chargeback (no catalogued suspect)
    s.merchant("M1008", "Krishna Sweets", "Ganesh Kulkarni", "Pune", "hi-Latn", "sweets", "2025-07-01", "88XXXX7093")
    _base_rental(s, "M1008", "D50008", "SB4G-6T3M84", "PLAN_SB499", "2025-07-03", shop="Krishna Sweets")
    cb_day, short_day = date(2026, 10, 28), date(2026, 11, 13)
    _daily(s, "M1008", win, date(2026, 11, 12), "D50008", (5, 10), (30, 800), card_share=0.03)
    cb_pay = s.payment("M1008", "D50008", "CARD", 234000, 2808, _ts(cb_day, 19, 5, 12))
    for amt, (h, m, sec) in zip(s.split(1822000, 16, lo=40), s.times(16)):
        s.payment("M1008", "D50008", "UPI", amt, 0, _ts(short_day, h, m, sec))
    s.t["chargebacks"].append(("CB-1008-01", "M1008", cb_pay, 234000, "GOODS_NOT_RECEIVED", "DEBITED",
                               "2026-11-06T11:30:00", "2026-11-13T18:05:00"))
    s.ledger("M1008", "CHARGEBACK_DEBIT", 234000, short_day, "2026-11-13T18:05:00", "chargeback", "CB-1008-01",
             "Chargeback adj. CB-1008-01")
    s.settle("M1008", extra={"2026-11-13": 234000})
    for doc in ("PAN", "AADHAAR", "GSTIN"):
        s.kyc("M1008", doc, verified="2025-07-01")
    s.kyc("M1008", "SHOP_LICENSE", verified="2025-07-01", expires="2027-06-30")


# ───────────────────────────── background population ─────────────────────────────

def _seed_background(s: _Seeder, n: int = 180) -> dict[str, int]:
    rng = s.rng
    ids = [f"M{2001 + i}" for i in range(n)]
    order = ids[:]
    rng.shuffle(order)
    zombie, healthy = set(order[:36]), set(order[36:56])
    dup_sub, pickup_failed = set(order[56:62]), set(order[62:70])
    normal = order[70:]
    risk, kyc = normal[:3], normal[3:8]
    loan_holders = sorted(rng.sample(ids, n // 4))
    dup_edi = set(rng.sample(loan_holders, 5))
    region_names = list(REGIONS)
    region_w = [REGIONS[r][0] for r in region_names]
    cat_names = list(CATEGORIES)
    cat_w = [CATEGORIES[c][0] for c in cat_names]
    win = TODAY - timedelta(days=30)

    for mid in ids:
        num = mid[1:]
        reg = REGIONS[rng.choices(region_names, region_w)[0]]
        city, lang = rng.choice(reg[1])
        first, last = rng.choice(reg[2]), rng.choice(reg[3])
        cat = rng.choices(cat_names, cat_w)[0]
        _, _, ticket, card_share = CATEGORIES[cat]
        name = s.business_name(cat, last, city)
        onboarded = TODAY - timedelta(days=rng.randint(150, 600))
        installed = onboarded + timedelta(days=rng.randint(1, 6))
        plan = "PLAN_SB499" if rng.random() < 0.7 else "PLAN_SB299"
        model = PLAN_MODEL[plan]
        did, sub, mdt = f"D5{num}", f"SUB-{num}-A", f"MDT-{num}-A"
        s.merchant(mid, name, f"{first} {last}", city, lang, cat, onboarded)
        s.event(did, "INSTALLED", _ts(installed, rng.randint(10, 18), rng.randint(0, 59)),
                f"INS-{installed.strftime('%y%m%d')}-{num}", f"Installed at {name}")
        used_until = LAST_DAY                                            # last day the device takes payments

        if mid in zombie or mid in healthy:
            if mid in zombie:                                            # picked up 1–3 months ago, never cancelled
                picked = TODAY - timedelta(days=rng.randint(31, 92))
                if picked.day == 1:
                    picked += timedelta(days=1)
            else:                                                        # healthy return: cancelled within days
                picked = TODAY - timedelta(days=rng.randint(20, 150))
                picked = picked.replace(day=min(max(picked.day, 3), 25))
            requested = picked - timedelta(days=rng.randint(5, 12))
            scheduled = requested + timedelta(days=rng.randint(1, 3))
            received = picked + timedelta(days=rng.randint(2, 5))
            tid, awb = f"PK-{requested.strftime('%y%m%d')}-{num}", f"DLV{rng.randint(10_000_000, 99_999_999)}"
            s.device(did, s.serial(model), model, None, "RETURNED", installed)
            s.event(did, "RETURN_REQUESTED", _ts(requested, rng.randint(9, 19), rng.randint(0, 59)), tid, "Merchant requested return in app")
            s.event(did, "PICKUP_SCHEDULED", _ts(scheduled, rng.randint(9, 12), rng.randint(0, 59)), tid,
                    f"Pickup slot {picked.day:02d} {MONTHS[picked.month - 1]} {picked.year}")
            s.event(did, "PICKED_UP", _ts(picked, rng.randint(11, 18), rng.randint(0, 59)), awb, "Collected by courier from the shop")
            s.event(did, "RECEIVED_AT_WAREHOUSE", _ts(received, rng.randint(10, 17), rng.randint(0, 59)), awb, "Received at warehouse; QC passed")
            s.ticket(tid, did, mid, "COMPLETED", _ts(requested, 10, 0), str(picked), _ts(picked, 15, 0), awb, 1, None)
            used_until = picked - timedelta(days=1)
            if mid in zombie:
                s.subscription(sub, mid, did, plan, "ACTIVE", installed)
                s.mandate(mdt, mid, "SUBSCRIPTION", sub=sub, max_paise=PLAN_FEE[plan] * 2, created=installed)
                s.rent(mid, sub, mdt, plan, _firsts(installed, LAST_DAY))
            else:
                ended = picked + timedelta(days=rng.randint(1, 3))
                s.event(did, "DEACTIVATED", _ts(ended, 9, rng.randint(0, 59)), sub, "Rental closed after return")
                s.subscription(sub, mid, did, plan, "CANCELLED", installed, ended, "DEVICE_RETURNED")
                s.mandate(mdt, mid, "SUBSCRIPTION", sub=sub, max_paise=PLAN_FEE[plan] * 2, status="REVOKED", created=installed)
                s.rent(mid, sub, mdt, plan, [d for d in _firsts(installed, LAST_DAY) if d <= picked])
        elif mid in pickup_failed:                                       # return requested, courier never got it
            requested = TODAY - timedelta(days=rng.randint(14, 45))
            tid = f"PK-{requested.strftime('%y%m%d')}-{num}"
            s.device(did, s.serial(model), model, mid, "RETURN_REQUESTED", installed)
            s.event(did, "RETURN_REQUESTED", _ts(requested, rng.randint(9, 19), rng.randint(0, 59)), tid, "Merchant requested return in app")
            s.event(did, "PICKUP_SCHEDULED", _ts(requested + timedelta(days=1), 10, rng.randint(0, 59)), tid,
                    "Pickup slot assigned")
            attempts = 1 if rng.random() < 0.4 else 2
            reasons = rng.sample(FAIL_REASONS, attempts)
            for k, reason in enumerate(reasons):
                s.event(did, "PICKUP_FAILED", _ts(requested + timedelta(days=3 + 3 * k), rng.randint(11, 18), rng.randint(0, 59)), tid, reason)
            s.ticket(tid, did, mid, "FAILED", _ts(requested, 10, 0), str(requested + timedelta(days=3 * attempts)), None,
                     f"DLV{rng.randint(10_000_000, 99_999_999)}", attempts, reasons[-1])
            s.subscription(sub, mid, did, plan, "ACTIVE", installed)
            s.mandate(mdt, mid, "SUBSCRIPTION", sub=sub, max_paise=PLAN_FEE[plan] * 2, created=installed)
            s.rent(mid, sub, mdt, plan, _firsts(installed, LAST_DAY))
        else:
            s.device(did, s.serial(model), model, mid, "ACTIVE", installed)
            s.subscription(sub, mid, did, plan, "ACTIVE", installed)
            s.mandate(mdt, mid, "SUBSCRIPTION", sub=sub, max_paise=PLAN_FEE[plan] * 2, created=installed)
            s.rent(mid, sub, mdt, plan, _firsts(installed, LAST_DAY))
            if mid in dup_sub:                                           # duplicate re-onboarding on the same device
                created = max(TODAY - timedelta(days=rng.randint(45, 160)), installed + timedelta(days=20))
                s.subscription(f"SUB-{num}-B", mid, did, plan, "ACTIVE", created)
                s.mandate(f"MDT-{num}-B", mid, "SUBSCRIPTION", sub=f"SUB-{num}-B", max_paise=PLAN_FEE[plan] * 2, created=created)
                s.rent(mid, f"SUB-{num}-B", f"MDT-{num}-B", plan, _firsts(created, LAST_DAY))

        # loans with daily EDI for the last 30 days
        if mid in loan_holders:
            principal = rng.choice([5000000, 7500000, 10000000, 15000000, 20000000, 30000000])
            tenure = rng.choice([180, 240, 300, 360])
            disbursed = TODAY - timedelta(days=rng.randint(35, 170))
            edi_amt = int(round(principal * 1.12 / tenure / 1000)) * 1000          # whole ₹10
            lender = rng.choice(LENDERS)
            s.loan(f"LN-{num}", mid, lender, principal, edi_amt, tenure, disbursed)
            s.mandate(f"MDT-{num}-L", mid, "LOAN_EDI", loan=f"LN-{num}", max_paise=edi_amt * 2, created=disbursed)
            dates = list(_days(max(win, disbursed + timedelta(days=1)), LAST_DAY))
            dup = {TODAY - timedelta(days=rng.randint(2, 20))} if mid in dup_edi else set()
            s.edi(mid, f"LN-{num}", f"MDT-{num}-L", lender, edi_amt, dates, dup_dates=dup, failures=True)

        # light payments for the last 30 days (static QR once the device is gone)
        for d in _days(win, LAST_DAY):
            k = rng.randint(0, 4)
            if k:
                s.day_payments(mid, d, did if d <= used_until else None, k, ticket, card_share, fail_p=0.02)

        # holds + KYC
        held: set[str] = set()
        if mid in risk:
            code, text = RISK_REASONS[risk.index(mid)]
            hd = TODAY - timedelta(days=rng.randint(1, 4))
            if not any(p[1] == mid and p[7][:10] == str(hd) and p[6] == "SUCCESS" for p in s.pay[-200:]):
                s.payment(mid, did, "CARD", s.amount(2000, 9000), 3000, _ts(hd, 20, 5))
            held = {str(hd)}
            s.hold(f"HLD-{num}-01", mid, f"STL-{num}-{hd.strftime('%Y%m%d')}", "RISK", code, text,
                   _ts(hd, 21, rng.randint(0, 59)))
        s.kyc(mid, "PAN", verified=onboarded)
        s.kyc(mid, "AADHAAR", verified=onboarded)
        if cat in ("electronics", "pharmacy", "apparel", "hardware") or rng.random() < 0.3:
            s.kyc(mid, "GSTIN", verified=onboarded)
        if mid in kyc:
            placed = TODAY - timedelta(days=rng.randint(1, 5))
            expired = placed - timedelta(days=1)
            doc = "DRUG_LICENSE" if cat == "pharmacy" else "SHOP_LICENSE"
            label = "Drug licence" if doc == "DRUG_LICENSE" else "Shop licence"
            s.kyc(mid, doc, "EXPIRED", verified=onboarded, expires=expired)
            held = {str(d) for d in _days(placed, LAST_DAY)}
            first_held = next((str(d) for d in _days(placed, LAST_DAY)
                               if any(p[1] == mid and p[7][:10] == str(d) and p[6] == "SUCCESS" for p in s.pay[-200:])), None)
            s.hold(f"HLD-{num}-01", mid, first_held and f"STL-{num}-{first_held.replace('-', '')}", "KYC", "KYC_DOC_EXPIRED",
                   f"{label} expired on {expired.day:02d} {MONTHS[expired.month - 1]} {expired.year}", _ts(placed, 6, 0))
        elif cat == "pharmacy":
            s.kyc(mid, "DRUG_LICENSE", verified=onboarded, expires=TODAY + timedelta(days=rng.randint(200, 1500)))
        elif rng.random() < 0.4:
            s.kyc(mid, "SHOP_LICENSE", verified=onboarded, expires=TODAY + timedelta(days=rng.randint(200, 1500)))
        s.settle(mid, held=held)

    # a few routine chargebacks elsewhere (open / won), so the table isn't scenario-only
    cards = [p for p in s.pay if p[3] == "CARD" and p[1] in normal and p[6] == "SUCCESS"]
    for k, p in enumerate(rng.sample(cards, min(6, len(cards)))):
        raised = date.fromisoformat(p[7][:10]) + timedelta(days=rng.randint(3, 9))
        s.t["chargebacks"].append((f"CB-{p[1][1:]}-{k + 1:02d}", p[1], p[0], p[4],
                                   rng.choice(["GOODS_NOT_RECEIVED", "DUPLICATE_PROCESSING", "FRAUD"]),
                                   rng.choice(["OPEN", "WON"]), _ts(min(raised, LAST_DAY), 11, 0), None))
    return {"zombie": len(zombie), "healthy_returns": len(healthy), "duplicate_subscription": len(dup_sub),
            "pickup_failed": len(pickup_failed), "loans": len(loan_holders), "duplicate_edi": len(dup_edi),
            "risk_holds": len(risk), "kyc_holds": len(kyc)}


# ───────────────────────────── stuck returns (seeded last, so every earlier row keeps its id and values) ─────────────

def _seed_stuck_returns(s: _Seeder, n: int = 10) -> dict[str, int]:
    """S9 plus background shops whose Soundbox return was requested but never collected (device idle since)."""
    rng = s.rng

    # S9 stuck_return ─ M1009 Pooja Fancy Store: return asked 22 Aug, the 25 Aug pickup never came, ₹299 rent still cut
    s.merchant("M1009", "Pooja Fancy Store", "Pooja Agarwal", "Kanpur", "hi", "kirana", "2025-12-01", "93XXXX6620")
    _base_rental(s, "M1009", "D50009", "SBLT-8H3R27", "PLAN_SB299", "2025-12-03", device_status="RETURN_REQUESTED",
                 shop="Pooja Fancy Store")
    s.event("D50009", "RETURN_REQUESTED", "2026-08-22T17:05:00", "PK-260822-1009", "Merchant requested return in app")
    s.event("D50009", "PICKUP_SCHEDULED", "2026-08-23T10:40:00", "PK-260822-1009", "Pickup slot 25 Aug 2026")
    s.ticket("PK-260822-1009", "D50009", "M1009", "SCHEDULED", "2026-08-22T17:05:00", "2026-08-25", None, None, 0, None)
    _daily(s, "M1009", date(2026, 8, 1), date(2026, 8, 21), "D50009", (2, 5), (20, 700))
    _daily(s, "M1009", date(2026, 8, 22), LAST_DAY, None, (2, 4), (20, 700))        # static QR since the return request
    s.settle("M1009")
    s.kyc("M1009", "PAN", verified="2025-12-01")
    s.kyc("M1009", "AADHAAR", verified="2025-12-01")

    # background: n shops, ids after the main population. #0 missed the courier itself (shop closed);
    # the last two asked only days ago (still inside the pickup window); the rest are stuck with us.
    region_names = list(REGIONS)
    region_w = [REGIONS[r][0] for r in region_names]
    cat_names = list(CATEGORIES)
    cat_w = [CATEGORIES[c][0] for c in cat_names]
    win = TODAY - timedelta(days=30)
    for i in range(n):
        mid = f"M{2181 + i}"
        num = mid[1:]
        reg = REGIONS[rng.choices(region_names, region_w)[0]]
        city, lang = rng.choice(reg[1])
        first, last = rng.choice(reg[2]), rng.choice(reg[3])
        cat = rng.choices(cat_names, cat_w)[0]
        _, _, ticket, card_share = CATEGORIES[cat]
        name = s.business_name(cat, last, city)
        onboarded = TODAY - timedelta(days=rng.randint(150, 600))
        installed = onboarded + timedelta(days=rng.randint(1, 6))
        plan = "PLAN_SB499" if rng.random() < 0.7 else "PLAN_SB299"
        model = PLAN_MODEL[plan]
        did, sub, mdt = f"D5{num}", f"SUB-{num}-A", f"MDT-{num}-A"
        recent = i >= n - 2
        requested = TODAY - timedelta(days=rng.randint(2, 5) if recent else rng.randint(26, 80))
        slot = requested + timedelta(days=rng.randint(2, 3))
        tid = f"PK-{requested.strftime('%y%m%d')}-{num}"
        s.merchant(mid, name, f"{first} {last}", city, lang, cat, onboarded)
        s.device(did, s.serial(model), model, mid, "RETURN_REQUESTED", installed)
        s.event(did, "INSTALLED", _ts(installed, rng.randint(10, 18), rng.randint(0, 59)),
                f"INS-{installed.strftime('%y%m%d')}-{num}", f"Installed at {name}")
        s.event(did, "RETURN_REQUESTED", _ts(requested, rng.randint(9, 19), rng.randint(0, 59)), tid, "Merchant requested return in app")
        s.event(did, "PICKUP_SCHEDULED", _ts(requested + timedelta(days=1), 10, rng.randint(0, 59)), tid,
                f"Pickup slot {slot.day:02d} {MONTHS[slot.month - 1]} {slot.year}")
        if i == 0:
            s.event(did, "PICKUP_FAILED", _ts(slot, 13, rng.randint(0, 59)), tid, "SHOP_CLOSED")
            s.ticket(tid, did, mid, "FAILED", _ts(requested, 10, 0), str(slot), None,
                     f"DLV{rng.randint(10_000_000, 99_999_999)}", 1, "SHOP_CLOSED")
        else:
            s.ticket(tid, did, mid, "SCHEDULED", _ts(requested, 10, 0), str(slot), None, None, 0, None)
        s.subscription(sub, mid, did, plan, "ACTIVE", installed)
        s.mandate(mdt, mid, "SUBSCRIPTION", sub=sub, max_paise=PLAN_FEE[plan] * 2, created=installed)
        s.rent(mid, sub, mdt, plan, _firsts(installed, LAST_DAY))
        for d in _days(win, LAST_DAY):                                   # static QR once the return was asked
            k = rng.randint(0, 4)
            if k:
                s.day_payments(mid, d, did if d < requested else None, k, ticket, card_share, fail_p=0.02)
        s.kyc(mid, "PAN", verified=onboarded)
        s.kyc(mid, "AADHAAR", verified=onboarded)
        s.settle(mid)
    return {"stuck_returns": n}


# ───────────────────────────── travel (seeded last, so every earlier row keeps its id and values) ─────────────
# Fictional operators, passengers, ids and amounts. Two complaint patterns seen in public posts, rebuilt with invented
# data: a bus that turned back but was never marked cancelled, and airline refunds that reached Paytm but not the passenger.

OPERATORS = [("OP-VEGA", "Vega Sleeper Lines", "BUS"), ("OP-NIMBUS", "Nimbus Air", "AIRLINE"),
             ("OP-KOEL", "Koel Skyways", "AIRLINE")]
OPERATOR_NAME = {o[0]: o[1] for o in OPERATORS}
BUS_FARE = {"L": 179000, "U": 164000}           # lower / upper sleeper berth

# S10 bus: (berth, account, name, booked_at, boarding scan). Kavya (U7) complains; the sweep finds the rest.
BUS_TRIP = "TR-VS2117-1109"
BUS_SEATS = [
    ("U7", "P2001", "Kavya R.", "2026-11-07T11:24:00", "2026-11-09T16:52:00"),
    ("L1", "P2101", "Sravani K.", "2026-11-01T09:12:00", "2026-11-09T16:41:00"),
    ("L2", "P2102", "Ravi Teja P.", "2026-11-01T19:40:00", "2026-11-09T16:43:00"),
    ("L4", "P2103", "Naresh B.", "2026-11-02T08:05:00", "2026-11-09T16:44:00"),
    ("L5", "P2104", "Anusha M.", "2026-11-03T13:30:00", "2026-11-09T16:46:00"),
    ("L7", "P2105", "Kiran Kumar S.", "2026-11-03T21:18:00", "2026-11-09T16:47:00"),
    ("L8", "P2106", "Padma V.", "2026-11-04T10:02:00", "2026-11-09T16:48:00"),
    ("L10", "P2107", "Harish N.", "2026-11-05T07:55:00", "2026-11-09T16:49:00"),
    ("L13", "P2108", "Deepa G.", "2026-11-05T18:21:00", "2026-11-09T16:50:00"),
    ("L14", "P2109", "Sai Kumar T.", "2026-11-06T12:44:00", "2026-11-09T16:51:00"),
    ("L16", "P2110", "Madhavi R.", "2026-11-07T09:36:00", "2026-11-09T16:53:00"),
    ("U2", "P2111", "Prasad Ch.", "2026-11-02T16:10:00", "2026-11-09T16:42:00"),
    ("U3", "P2112", "Lakshmi D.", "2026-11-03T11:27:00", "2026-11-09T16:44:00"),
    ("U5", "P2113", "Manjunath H.", "2026-11-04T20:05:00", "2026-11-09T16:45:00"),
    ("U9", "P2114", "Shilpa B.", "2026-11-05T14:48:00", "2026-11-09T16:50:00"),
    ("U10", "P2115", "Ramana K.", "2026-11-06T08:33:00", "2026-11-09T16:52:00"),
    ("U12", "P2116", "Nagesh P.", "2026-11-06T22:14:00", "2026-11-09T16:54:00"),
    ("U15", "P2117", "Swathi Y.", "2026-11-07T15:02:00", "2026-11-09T16:55:00"),
    ("U17", "P2118", "Venkatesh A.", "2026-11-08T10:40:00", "2026-11-09T16:57:00"),
    ("L11", "P2119", "Gopal S.", "2026-11-02T10:15:00", None),                     # cancelled 8 Nov, refunded then
    ("U13", "P2120", "Rekha N.", "2026-11-04T17:50:00", "2026-11-09T16:51:00"),    # operator refunded 10 Nov
    ("L17", "P2121", "Arun V.", "2026-11-08T21:05:00", None),                      # never boarded: no-show rule
]

# S11 flights: (trip id, operator, flight no, from, to, departs, cancelled at)
FLIGHTS = [
    ("TR-NB615-0921", "OP-NIMBUS", "NB 615", "Delhi", "Bengaluru", "2026-09-21T07:40:00", "2026-09-21T05:10:00"),
    ("TR-KS208-0928", "OP-KOEL", "KS 208", "Hyderabad", "Mumbai", "2026-09-28T18:15:00", "2026-09-28T12:30:00"),
    ("TR-NB233-1004", "OP-NIMBUS", "NB 233", "Mumbai", "Kolkata", "2026-10-04T09:05:00", "2026-10-03T22:40:00"),
    ("TR-KS512-1012", "OP-KOEL", "KS 512", "Chennai", "Pune", "2026-10-12T14:50:00", "2026-10-12T09:15:00"),
    ("TR-NB104-1015", "OP-NIMBUS", "NB 104", "Jaipur", "Delhi", "2026-10-15T06:30:00", "2026-10-14T23:05:00"),
    ("TR-KS208-1019", "OP-KOEL", "KS 208", "Hyderabad", "Mumbai", "2026-10-19T18:15:00", "2026-10-19T11:50:00"),
    ("TR-NB441-1026", "OP-NIMBUS", "NB 441", "Kochi", "Delhi", "2026-10-26T10:20:00", "2026-10-26T04:45:00"),
    ("TR-NB902-1024", "OP-NIMBUS", "NB 902", "Lucknow", "Bengaluru", "2026-10-24T08:00:00", "2026-10-23T20:30:00"),
    ("TR-KS117-1030", "OP-KOEL", "KS 117", "Pune", "Delhi", "2026-10-30T19:40:00", "2026-10-30T13:00:00"),
    ("TR-NB790-1102", "OP-NIMBUS", "NB 790", "Bengaluru", "Ahmedabad", "2026-11-02T16:10:00", "2026-11-02T09:20:00"),
]
# (booking id, account, name, trip, pnr, pax, fare, booked_at, airline paid Paytm at, passed on to the passenger at)
FLIGHT_BOOKINGS = [
    ("PT-FLT-5520918", "P2002", "Arjun M.", "TR-NB615-0921", "Q4M7XK", 2, 2186000, "2026-09-02T20:16:00", "2026-10-03T14:22:00", None),
    ("PT-FLT-5527341", "P2201", "Neha S.", "TR-NB615-0921", "H7T2PA", 2, 1840900, "2026-09-04T09:30:00", "2026-10-03T14:22:00", None),
    ("PT-FLT-5561077", "P2202", "Rahul K.", "TR-NB615-0921", "ZK9W4M", 1, 1093000, "2026-09-09T22:05:00", "2026-10-03T14:22:00", None),
    ("PT-FLT-5618402", "P2203", "Fatima Z.", "TR-KS208-0928", "R2B8NQ", 1, 621500, "2026-09-12T13:44:00", "2026-10-09T11:05:00", None),
    ("PT-FLT-5703619", "P2204", "Vikram J.", "TR-NB233-1004", "M6X3TD", 2, 987000, "2026-09-18T18:20:00", "2026-10-17T15:40:00", None),
    ("PT-FLT-5822964", "P2205", "Ananya P.", "TR-KS512-1012", "F9L2VC", 1, 346000, "2026-09-27T08:12:00", "2026-10-24T10:30:00", None),
    ("PT-FLT-5857090", "P2206", "Rohan D.", "TR-NB104-1015", "W3P8KJ", 1, 482000, "2026-10-01T19:55:00", "2026-10-29T16:15:00", None),
    ("PT-FLT-5931538", "P2207", "Meera T.", "TR-KS208-1019", "B5N7RY", 1, 710500, "2026-10-06T07:48:00", "2026-10-31T12:00:00", None),
    ("PT-FLT-6004725", "P2208", "Siddharth G.", "TR-NB441-1026", "T8D4GH", 1, 554000, "2026-10-10T21:33:00", "2026-11-05T13:25:00", None),
    ("PT-FLT-6012456", "P2209", "Priya L.", "TR-NB902-1024", "C2V9ML", 1, 612000, "2026-10-08T11:10:00", "2026-11-04T10:05:00", "2026-11-06T09:40:00"),
    ("PT-FLT-6088113", "P2210", "Karan B.", "TR-KS117-1030", "J7Q3XS", 1, 498000, "2026-10-14T16:02:00", None, None),
    ("PT-FLT-6061287", "P2211", "Isha V.", "TR-NB790-1102", "N4F6WB", 1, 539000, "2026-10-19T10:45:00", "2026-11-11T15:50:00", None),
]


def _seed_travel(s: _Seeder) -> dict[str, int]:
    """S10 bus_turned_back (P2001), S11 flight_refund (P2002) and the other passengers on the same trips."""
    s.t["operators"].extend(OPERATORS)
    fixed = {"P2001": ("Vijayawada", "te", "94XXXX3187"), "P2002": ("Delhi", "en", "98XXXX4410")}
    n_phone = 0

    def passenger(pid: str, name: str, lang: str, town: str) -> None:
        nonlocal n_phone
        if pid in fixed:
            town, lang, phone = fixed[pid]
        else:
            n_phone += 1
            phone = f"9{(n_phone * 7) % 10}XXXX{4000 + n_phone * 37}"
        s.merchant(pid, name, name, town, lang, "passenger", "2025-06-01", phone)

    def fare_debit(pid: str, bid: str, amount: int, at: str, what: str) -> int:
        return s.ledger(pid, "BOOKING_DEBIT", amount, at[:10], at, "booking", bid, what)

    def refund(pid: str, bid: str, amount: int, at: str, of: int, why: str) -> None:
        s.ledger(pid, "REFUND_CREDIT", amount, at[:10], at, "booking", bid, why, refund_of=of)

    def trip_event(trip: str, source: str, etype: str, at: str, km=None, note=None) -> None:
        s.t["trip_events"].append((s.next("trip_event"), trip, source, etype, at, km, note))

    # ── S10: Vega Sleeper Lines VS-2117, Vijayawada → Bengaluru, 9 Nov 17:00. Turned back at km 38; status still DEPARTED.
    s.t["trips"].append((BUS_TRIP, "OP-VEGA", "BUS", "VS-2117", "Vijayawada", "Bengaluru", "2026-11-09T17:00:00",
                         "2026-11-10T06:50:00", "DEPARTED", "2026-11-09T17:06:00"))
    trip_event(BUS_TRIP, "GPS", "DEPARTED", "2026-11-09T17:06:00", 0, "Left Vijayawada")
    trip_event(BUS_TRIP, "GPS", "STOPPED", "2026-11-09T17:51:00", 38, "Stopped 47 min")
    trip_event(BUS_TRIP, "GPS", "TURNED_BACK", "2026-11-09T18:38:00", 38, "Heading back to Vijayawada")
    trip_event(BUS_TRIP, "DRIVER", "REPORT", "2026-11-09T19:10:00", 38, "Fuel ran out near km 38. Passengers taken back.")
    trip_event(BUS_TRIP, "GPS", "RETURNED_TO_ORIGIN", "2026-11-09T19:34:00", 0, "Back at Vijayawada")
    for i, (seat, pid, name, booked, boarded) in enumerate(BUS_SEATS):
        bid = "PT-BUS-7731042" if pid == "P2001" else f"PT-BUS-77{31100 + i * 7:05d}"
        fare = BUS_FARE[seat[0]]
        passenger(pid, name, "te" if i % 3 else "kn", "Vijayawada" if i % 4 else "Guntur")
        lid = fare_debit(pid, bid, fare, booked, f"Bus ticket {bid} · Vega Sleeper Lines VS-2117 · 09 Nov · {seat}")
        status, cancelled = ("CANCELLED", "2026-11-08T20:14:00") if seat == "L11" else ("CONFIRMED", None)
        s.t["bookings"].append((bid, pid, BUS_TRIP, name, seat, None, 1, fare, status, booked, boarded, cancelled, lid))
        if seat == "L11":
            refund(pid, bid, fare, "2026-11-08T20:16:00", lid, f"Refund of Bus ticket {bid} · cancelled by passenger")
        if seat == "U13":
            refund(pid, bid, fare, "2026-11-10T12:05:00", lid, f"Refund of Bus ticket {bid} · by Vega Sleeper Lines")
    s.t["support_calls"].append(("SC-261110-2001", "P2001", "PT-BUS-7731042", "2026-11-10T10:30:00",
                                 "Passenger says the bus turned back after the fuel ran out. Told: refund in 3 days."))

    # ── S11: cancelled flights. The airline paid Paytm back; most payments came in with the PNR only and were never matched.
    flights = {f[0]: f for f in FLIGHTS}
    for trip, op, no, frm, to, dep, cancelled_at in FLIGHTS:
        arr = f"{dep[:11]}{(int(dep[11:13]) + 2) % 24:02d}{dep[13:]}"
        s.t["trips"].append((trip, op, "FLIGHT", no, frm, to, dep, arr, "CANCELLED", cancelled_at))
        trip_event(trip, "AIRLINE", "CANCELLED", cancelled_at, None, f"Cancelled by {OPERATOR_NAME[op]}: operational reasons")
    for bid, pid, name, trip, pnr, pax, fare, booked, paid, passed in FLIGHT_BOOKINGS:
        _, op, no, *_rest = flights[trip]
        passenger(pid, name, "en", "Delhi")
        lid = fare_debit(pid, bid, fare, booked, f"Flight {bid} · {OPERATOR_NAME[op]} {no} · PNR {pnr}")
        s.t["bookings"].append((bid, pid, trip, name, None, pnr, pax, fare, "CANCELLED", booked, None, flights[trip][6], lid))
        if paid:
            s.t["airline_refunds"].append((f"ARF-{paid[2:10].replace('-', '')}-{pnr}", op, pnr, name.upper(), fare, paid,
                                           bid if passed else None, "PASSED_ON" if passed else "UNMATCHED"))
        if passed:
            refund(pid, bid, fare, passed, lid, f"Refund of Flight {bid} · {no} cancelled by the airline")
    s.t["support_calls"].append(("SC-260929-2002", "P2002", "PT-FLT-5520918", "2026-09-29T12:10:00",
                                 "Passenger asked about the NB 615 refund. Told: waiting for Nimbus Air to pay."))
    s.t["support_calls"].append(("SC-261020-2002", "P2002", "PT-FLT-5520918", "2026-10-20T18:45:00",
                                 "Passenger called again about the refund. Told: still waiting for the airline."))
    return {"passengers": len(BUS_SEATS) + len(FLIGHT_BOOKINGS)}


# ───────────────────────────── public API ─────────────────────────────

def build_rows() -> dict[str, list[tuple]]:
    """All business rows, deterministic (no DB access)."""
    s = _Seeder()
    s.t["plans"].extend(PLANS)
    _seed_scenarios(s)
    _seed_background(s)
    _seed_stuck_returns(s)
    _seed_travel(s)
    s.t["payments"] = [tuple(p) for p in s.pay]
    return s.t


def _remove_files(path: Path) -> bool:
    """Delete db + -wal/-shm. False if the main file is locked (Windows: another connection holds it)."""
    gc.collect()                                     # finalise stray sqlite3.Connection objects in this process
    try:
        if path.exists():
            path.unlink()
    except PermissionError:
        return False
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)
        try:
            if side.exists():
                side.unlink()
        except PermissionError:
            pass
    return True


def _drop_all(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for name in names:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def reset_db(db_path: str | Path | None = None) -> dict[str, int]:
    """Delete the db (+ -wal/-shm), create the schema, seed it. Returns row counts per table."""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    removed = _remove_files(path)
    data = build_rows()
    conn = connect(path)
    try:
        if not removed:                              # file locked: rebuild in place instead
            _drop_all(conn)
        create_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            for table, cols in COLUMNS.items():
                if data[table]:
                    marks = ",".join("?" * len(cols))
                    conn.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES ({marks})", data[table])
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (*COLUMNS, *WORKFLOW_TABLES)}
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return counts


def main() -> None:
    t0 = time.perf_counter()
    counts = reset_db()
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"Seeded {config.DB_PATH} in {ms} ms")
    for t, c in counts.items():
        print(f"  {t:<16} {c:>6}")
    try:
        from munim.db.conn import connect as _c
        from munim.forensics.suspects import find_victims
        from munim.models import rupees
        conn = _c(config.DB_PATH)
        for sid in ("ZOMBIE_BILLING", "RETURN_STUCK", "DUPLICATE_SUBSCRIPTION", "DUPLICATE_EDI",
                    "BUS_TRIP_FAILED", "AIRLINE_REFUND_NOT_PASSED"):
            v = find_victims(conn, sid, None, config.DEMO_TODAY)
            print(f"  sweep {sid:<24} {len(v):>3} merchants  {rupees(sum(x.damages_paise for x in v))}")
        conn.close()
    except Exception as e:  # pragma: no cover - diagnostics only
        print(f"  (sweep check skipped: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
