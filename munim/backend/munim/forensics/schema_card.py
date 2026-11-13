"""Compact, LLM-facing description of the business tables for the improvise step (read-only SQL probes)."""
from __future__ import annotations

from munim import config

_CARD = """\
SQLite database of one payments company's merchant accounts. You may read ONLY these business tables.
Money is INTEGER paise everywhere (*_paise columns): 49900 = Rs 499.00. Dates/times are ISO text
('2026-11-13', '2026-11-13T18:05:00'); compare them as strings, e.g. value_date = '2026-11-13'.

TABLES (key columns; -> = foreign key)
merchants(id 'M1008', business_name, owner_name, phone(masked), city, language, category, onboarded_at, status)
plans(id 'PLAN_SB499', name, device_model, monthly_fee_paise)                                   -- no merchant_id
devices(id 'D50001', serial_no, model, merchant_id -> merchants NULL once returned, status, installed_at)
    status: ACTIVE | RETURN_REQUESTED | RETURNED | REPLACED | LOST
device_events(id, device_id -> devices, event_type, at, ref, note)                               -- no merchant_id
    event_type: INSTALLED | RETURN_REQUESTED | PICKUP_SCHEDULED | PICKUP_FAILED | PICKED_UP | RECEIVED_AT_WAREHOUSE
                | DEACTIVATED | SWAPPED_OUT | SWAPPED_IN;  ref = pickup ticket id or courier AWB
pickup_tickets(id, device_id, merchant_id, status OPEN|SCHEDULED|COMPLETED|FAILED|CANCELLED, requested_at,
               scheduled_for, completed_at, courier_awb, attempts, failure_reason)
subscriptions(id 'SUB-1001-A', merchant_id, device_id, plan_id -> plans, status ACTIVE|CANCELLED|PAUSED,
              started_at, ended_at, end_reason)                     -- Soundbox rental, billed monthly via a mandate
loans(id 'LN-1003', merchant_id, lender, principal_paise, edi_amount_paise (daily instalment), tenure_days,
      status ACTIVE|CLOSED, disbursed_at)
mandates(id, merchant_id, purpose SUBSCRIPTION|LOAN_EDI, subscription_id, loan_id, umrn, max_amount_paise,
         status ACTIVE|REVOKED, created_at)
edi_attempts(id, loan_id -> loans, due_date, attempt_no, gateway_ref, status SUCCESS|FAILED|TIMEOUT, responded_at,
             ledger_entry_id -> ledger_entries)          -- no merchant_id; a TIMEOUT can still have debited
settlements(id 'STL-1008-20261113', merchant_id, batch_date, gross_paise, deductions_paise, net_paise,
            status SETTLED|HELD|RELEASED, utr, settled_at)   -- daily payout; net = gross - deductions
payments(id, merchant_id, device_id, mode UPI|CARD, amount_paise, mdr_paise, status SUCCESS|FAILED, paid_at,
         settlement_id -> settlements)                   -- customer payments; prove a device is still in use
chargebacks(id 'CB-...', merchant_id, payment_id -> payments, amount_paise, reason, status OPEN|DEBITED|WON|LOST,
            raised_at, debited_at)
holds(id 'HLD-...', merchant_id, settlement_id, hold_type RISK|KYC|BANK_ACCOUNT, reason_code, reason_text,
      status ACTIVE|RELEASED, placed_at, released_at)
kyc_documents(id, merchant_id, doc_type PAN|AADHAAR|GSTIN|DRUG_LICENSE|SHOP_LICENSE,
              status VALID|EXPIRED|PENDING|REJECTED, verified_at, expires_at)
ledger_entries(id, merchant_id, txn_ref, entry_type, direction DEBIT|CREDIT, amount_paise (always > 0),
               status SUCCESS|FAILED|REVERSED, source_type, source_id, mandate_id, value_date, posted_at, narration,
               refund_of -> ledger_entries.id)           -- every rupee in or out; narration = passbook text
    entry_type: RENT_DEBIT | EDI_DEBIT | SETTLEMENT_CREDIT | REFUND_CREDIT | CHARGEBACK_DEBIT
    source_type/source_id: subscription/SUB-..., loan/LN-..., settlement/STL-..., chargeback/CB-..., action/<id>

JOINS
ledger_entries.source_id = subscriptions.id | loans.id | settlements.id | chargebacks.id  (by source_type)
payments.settlement_id = settlements.id;  chargebacks.payment_id = payments.id
edi_attempts.ledger_entry_id = ledger_entries.id;  refund: ledger_entries r WHERE r.refund_of = <debit id>
Settlement deductions = SUM(payments.mdr_paise) of the batch + chargebacks/adjustments posted that day.

RULES (enforced; violations are rejected)
- Exactly ONE statement: SELECT or WITH ... SELECT. No INSERT/UPDATE/DELETE/PRAGMA/ATTACH/CREATE, no sqlite_* tables.
- Every query MUST filter by the merchant as a quoted literal: merchant_id = '<MERCHANT_ID>'. Tables without
  merchant_id (device_events, edi_attempts, plans) are reached by joining devices / loans of that merchant.
- Never mention another merchant id. Other merchants' rows and workflow tables (cases, actions, audit_log, ...) are
  invisible to probes.
- At most {row_limit} rows come back and each query has a ~{time_cap:g} s time cap: aggregate, filter by date, use LIMIT.
- Select ledger_entries.id AS ledger_id when a ledger row might be evidence; only ids seen in results count.

EXAMPLE 1: what made up a day's settlement deductions (MDR vs anything else)
SELECT s.id, s.batch_date, s.gross_paise, s.deductions_paise, s.net_paise, s.status, s.utr,
       (SELECT COUNT(*) FROM payments p WHERE p.settlement_id = s.id AND p.merchant_id = '<MERCHANT_ID>') AS payment_count,
       (SELECT COALESCE(SUM(p.mdr_paise), 0) FROM payments p
         WHERE p.settlement_id = s.id AND p.merchant_id = '<MERCHANT_ID>') AS mdr_paise
FROM settlements s
WHERE s.merchant_id = '<MERCHANT_ID>' AND s.batch_date = '2026-11-13';

EXAMPLE 2: every ledger movement on a day, with any chargeback behind it
SELECT l.id AS ledger_id, l.entry_type, l.direction, l.amount_paise, l.value_date, l.narration,
       c.id AS chargeback_id, c.reason, c.status AS chargeback_status, p.mode, p.paid_at
FROM ledger_entries l
LEFT JOIN chargebacks c ON l.source_type = 'chargeback' AND c.id = l.source_id
LEFT JOIN payments p ON p.id = c.payment_id
WHERE l.merchant_id = '<MERCHANT_ID>' AND l.value_date = '2026-11-13'
ORDER BY l.id;
"""


def schema_card() -> str:
    """Business tables, columns, joins, enums, paise, the merchant-scoping rule and two example queries."""
    from munim.forensics import probe
    return _CARD.replace("{row_limit}", str(config.PROBE_ROW_LIMIT)).replace("{time_cap:g}", f"{probe.TIME_LIMIT_S:g}")
