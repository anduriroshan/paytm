"""Deterministic demo data: the 8 scenario merchants (full histories) + ~180 background merchants.

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
                  "REFUND_CREDIT": "RFD", "CHARGEBACK_DEBIT": "CBK"}[etype]
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


# ───────────────────────────── public API ─────────────────────────────

def build_rows() -> dict[str, list[tuple]]:
    """All business rows, deterministic (no DB access)."""
    s = _Seeder()
    s.t["plans"].extend(PLANS)
    _seed_scenarios(s)
    _seed_background(s)
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
        for sid in ("ZOMBIE_BILLING", "DUPLICATE_SUBSCRIPTION", "DUPLICATE_EDI"):
            v = find_victims(conn, sid, None, config.DEMO_TODAY)
            print(f"  sweep {sid:<24} {len(v):>3} merchants  {rupees(sum(x.damages_paise for x in v))}")
        conn.close()
    except Exception as e:  # pragma: no cover - diagnostics only
        print(f"  (sweep check skipped: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
