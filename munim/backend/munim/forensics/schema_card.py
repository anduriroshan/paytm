"""Compact, LLM-facing description of the business tables for the improvise step (read-only SQL probes).

Built from the connected database sources (backend/sources/*.yaml, `tables:` → `about:`): a source that is turned off
disappears from the card, and its tables from what probes may read. Joins and examples are shown only when every table
they use is connected.
"""
from __future__ import annotations

from typing import Iterable

from munim import config

# card order (not alphabetical: related tables sit together)
CARD_ORDER = ("merchants", "plans", "devices", "device_events", "pickup_tickets", "subscriptions", "loans", "mandates",
              "edi_attempts", "settlements", "payments", "chargebacks", "holds", "kyc_documents", "ledger_entries",
              "operators", "trips", "bookings", "trip_events", "airline_refunds", "support_calls")

_HEAD = """\
SQLite database of one payments company's merchant accounts. You may read ONLY these business tables.
Money is INTEGER paise everywhere (*_paise columns): 49900 = Rs 499.00. Dates/times are ISO text
('2026-11-13', '2026-11-13T18:05:00'); compare them as strings, e.g. value_date = '2026-11-13'.
"""

# (line, tables it needs)
_JOINS: list[tuple[str, set[str]]] = [
    ("ledger_entries.source_id = subscriptions.id | loans.id | settlements.id | chargebacks.id  (by source_type)",
     {"ledger_entries"}),
    ("payments.settlement_id = settlements.id;  chargebacks.payment_id = payments.id", {"payments"}),
    ("edi_attempts.ledger_entry_id = ledger_entries.id;  refund: ledger_entries r WHERE r.refund_of = <debit id>",
     {"ledger_entries"}),
    ("Settlement deductions = SUM(payments.mdr_paise) of the batch + chargebacks/adjustments posted that day.",
     {"settlements", "payments"}),
    ("bookings.passenger_id = merchants.id (category 'passenger');  bookings.trip_id = trips.id;  "
     "bookings.ledger_entry_id = the fare (BOOKING_DEBIT);  trip_events.trip_id = trips.id;  "
     "airline_refunds.booking_id = bookings.id (NULL = never matched; match by pnr)", {"bookings"}),
]

_RULES = """\
RULES (enforced; violations are rejected)
- Exactly ONE statement: SELECT or WITH ... SELECT. No INSERT/UPDATE/DELETE/PRAGMA/ATTACH/CREATE, no sqlite_* tables.
- Every query MUST filter by the merchant as a quoted literal: merchant_id = '<MERCHANT_ID>'. Tables without
  merchant_id (device_events, edi_attempts, plans) are reached by joining devices / loans of that merchant.
- Never mention another merchant id. Other merchants' rows and workflow tables (cases, actions, audit_log, ...) are
  invisible to probes.
- At most {row_limit} rows come back and each query has a ~{time_cap} s time cap: aggregate, filter by date, use LIMIT.
- Select ledger_entries.id AS ledger_id when a ledger row might be evidence; only ids seen in results count.
"""

_EXAMPLES: list[tuple[str, set[str]]] = [
    ("""\
EXAMPLE 1: what made up a day's settlement deductions (MDR vs anything else)
SELECT s.id, s.batch_date, s.gross_paise, s.deductions_paise, s.net_paise, s.status, s.utr,
       (SELECT COUNT(*) FROM payments p WHERE p.settlement_id = s.id AND p.merchant_id = '<MERCHANT_ID>') AS payment_count,
       (SELECT COALESCE(SUM(p.mdr_paise), 0) FROM payments p
         WHERE p.settlement_id = s.id AND p.merchant_id = '<MERCHANT_ID>') AS mdr_paise
FROM settlements s
WHERE s.merchant_id = '<MERCHANT_ID>' AND s.batch_date = '2026-11-13';
""", {"settlements", "payments"}),
    ("""\
EXAMPLE 2: every ledger movement on a day, with any chargeback behind it
SELECT l.id AS ledger_id, l.entry_type, l.direction, l.amount_paise, l.value_date, l.narration,
       c.id AS chargeback_id, c.reason, c.status AS chargeback_status, p.mode, p.paid_at
FROM ledger_entries l
LEFT JOIN chargebacks c ON l.source_type = 'chargeback' AND c.id = l.source_id
LEFT JOIN payments p ON p.id = c.payment_id
WHERE l.merchant_id = '<MERCHANT_ID>' AND l.value_date = '2026-11-13'
ORDER BY l.id;
""", {"ledger_entries", "chargebacks", "payments"}),
]


def _about(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def schema_card(hits: Iterable | None = None) -> str:
    """Connected tables (columns, enums, notes), joins, the merchant-scoping rule and example queries.
    hits: this case's SourceHit list; adds which connected tables matter most for the complaint."""
    from munim.forensics import probe
    from munim.sources import current
    reg = current()
    about = reg.table_about()
    tables = [t for t in CARD_ORDER if t in about]
    parts = [_HEAD, "\n"]
    if hits:
        focus = [t for h in hits if getattr(h, "reached", False) and getattr(h, "kind", "") == "database"
                 for t in getattr(h, "tables", []) if t in about]
        if focus:
            parts.append(f"MOST RELEVANT TO THIS COMPLAINT: {', '.join(dict.fromkeys(focus))}\n\n")
    parts.append("TABLES (key columns; -> = foreign key)\n")
    parts += [_about(about[t]) for t in tables] or ["(none: no database source is turned on)\n"]
    joins = [line for line, need in _JOINS if need <= set(tables)]
    if joins:
        parts.append("\nJOINS\n" + "".join(j + "\n" for j in joins))
    parts.append("\n" + _RULES.replace("{row_limit}", str(config.PROBE_ROW_LIMIT))
                 .replace("{time_cap}", f"{probe.TIME_LIMIT_S:g}"))
    parts += ["\n" + ex for ex, need in _EXAMPLES if need <= set(tables)]
    return "".join(parts)
