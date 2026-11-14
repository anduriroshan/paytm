-- Munim demo database (SQLite). Mirrors the Paytm systems an L1 agent reads today:
-- devices + pickups, rental subscriptions, mandates, loans + EDI, payments, settlements + holds, KYC, ledger.
-- Money is always INTEGER paise. Timestamps are ISO-8601 text ('2026-11-14' or '2026-11-14T10:30:00').
-- Workflow tables (cases, actions, sweeps, notifications, audit_log) are written by Munim itself.
PRAGMA foreign_keys = ON;

-- ───────────────────────────── business data (mocked Paytm systems) ─────────────────────────────

CREATE TABLE merchants (
  id            TEXT PRIMARY KEY,              -- 'M1001'
  business_name TEXT NOT NULL,
  owner_name    TEXT NOT NULL,
  phone         TEXT NOT NULL,                 -- masked demo number, e.g. '98XXXX1234'
  city          TEXT NOT NULL,
  language      TEXT NOT NULL,                 -- preferred reply language: 'hi' | 'hi-Latn' | 'te' | 'en' | 'ta' | 'mr' ...
  category      TEXT NOT NULL,                 -- 'kirana' | 'pharmacy' | 'restaurant' | 'electronics' | ...
  onboarded_at  TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'ACTIVE' -- ACTIVE | SUSPENDED
);

CREATE TABLE plans (
  id                TEXT PRIMARY KEY,          -- 'PLAN_SB499'
  name              TEXT NOT NULL,             -- 'Soundbox 4G Rental'
  device_model      TEXT NOT NULL,
  monthly_fee_paise INTEGER NOT NULL
);

CREATE TABLE devices (
  id           TEXT PRIMARY KEY,               -- 'D50001'
  serial_no    TEXT NOT NULL UNIQUE,           -- 'SB4G-7Q2K91'
  model        TEXT NOT NULL,
  merchant_id  TEXT REFERENCES merchants(id),  -- NULL once back in the warehouse pool
  status       TEXT NOT NULL,                  -- ACTIVE | RETURN_REQUESTED | RETURNED | REPLACED | LOST
  installed_at TEXT
);

CREATE TABLE device_events (                   -- device lifecycle, append-only
  id         INTEGER PRIMARY KEY,
  device_id  TEXT NOT NULL REFERENCES devices(id),
  event_type TEXT NOT NULL,  -- INSTALLED | RETURN_REQUESTED | PICKUP_SCHEDULED | PICKUP_FAILED | PICKED_UP
                             -- | RECEIVED_AT_WAREHOUSE | DEACTIVATED | SWAPPED_OUT | SWAPPED_IN
  at         TEXT NOT NULL,
  ref        TEXT,           -- pickup ticket id / courier AWB / swap ticket
  note       TEXT
);

CREATE TABLE pickup_tickets (
  id             TEXT PRIMARY KEY,             -- 'PK-240912-0042'
  device_id      TEXT NOT NULL REFERENCES devices(id),
  merchant_id    TEXT NOT NULL REFERENCES merchants(id),
  status         TEXT NOT NULL,                -- OPEN | SCHEDULED | COMPLETED | FAILED | CANCELLED
  requested_at   TEXT NOT NULL,
  scheduled_for  TEXT,
  completed_at   TEXT,
  courier_awb    TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT                          -- 'SHOP_CLOSED' | 'MERCHANT_UNAVAILABLE' | ...
);

CREATE TABLE subscriptions (                   -- device rental plans; billed monthly via mandate
  id          TEXT PRIMARY KEY,                -- 'SUB-1001-A'
  merchant_id TEXT NOT NULL REFERENCES merchants(id),
  device_id   TEXT REFERENCES devices(id),
  plan_id     TEXT NOT NULL REFERENCES plans(id),
  status      TEXT NOT NULL,                   -- ACTIVE | CANCELLED | PAUSED
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  end_reason  TEXT
);

CREATE TABLE loans (
  id               TEXT PRIMARY KEY,           -- 'LN-1003'
  merchant_id      TEXT NOT NULL REFERENCES merchants(id),
  lender           TEXT NOT NULL,              -- NBFC partner name (fictional)
  principal_paise  INTEGER NOT NULL,
  edi_amount_paise INTEGER NOT NULL,           -- equated DAILY instalment
  tenure_days      INTEGER NOT NULL,
  status           TEXT NOT NULL,              -- ACTIVE | CLOSED
  disbursed_at     TEXT NOT NULL
);

CREATE TABLE mandates (                        -- auto-debit authorisations
  id               TEXT PRIMARY KEY,           -- 'MDT-...'
  merchant_id      TEXT NOT NULL REFERENCES merchants(id),
  purpose          TEXT NOT NULL,              -- SUBSCRIPTION | LOAN_EDI
  subscription_id  TEXT REFERENCES subscriptions(id),
  loan_id          TEXT REFERENCES loans(id),
  umrn             TEXT NOT NULL UNIQUE,
  max_amount_paise INTEGER NOT NULL,
  status           TEXT NOT NULL,              -- ACTIVE | REVOKED
  created_at       TEXT NOT NULL
);

CREATE TABLE settlements (                     -- daily payout batches to the merchant's bank
  id               TEXT PRIMARY KEY,           -- 'STL-1005-20261111'
  merchant_id      TEXT NOT NULL REFERENCES merchants(id),
  batch_date       TEXT NOT NULL,
  gross_paise      INTEGER NOT NULL,           -- sum of payments in the batch
  deductions_paise INTEGER NOT NULL,           -- MDR + chargebacks + adjustments
  net_paise        INTEGER NOT NULL,
  status           TEXT NOT NULL,              -- SETTLED | HELD | RELEASED
  utr              TEXT,                       -- bank reference once paid
  settled_at       TEXT
);

CREATE TABLE payments (                        -- customer payments received by the merchant (proves device usage)
  id            INTEGER PRIMARY KEY,
  merchant_id   TEXT NOT NULL REFERENCES merchants(id),
  device_id     TEXT REFERENCES devices(id),   -- Soundbox/EDC that announced or accepted it
  mode          TEXT NOT NULL,                 -- UPI | CARD
  amount_paise  INTEGER NOT NULL,
  mdr_paise     INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL,                 -- SUCCESS | FAILED
  paid_at       TEXT NOT NULL,
  settlement_id TEXT REFERENCES settlements(id)
);

CREATE TABLE chargebacks (
  id           TEXT PRIMARY KEY,               -- 'CB-...'
  merchant_id  TEXT NOT NULL REFERENCES merchants(id),
  payment_id   INTEGER NOT NULL REFERENCES payments(id),
  amount_paise INTEGER NOT NULL,
  reason       TEXT NOT NULL,                  -- 'GOODS_NOT_RECEIVED' | 'FRAUD' | ...
  status       TEXT NOT NULL,                  -- OPEN | DEBITED | WON | LOST
  raised_at    TEXT NOT NULL,
  debited_at   TEXT
);

CREATE TABLE holds (
  id            TEXT PRIMARY KEY,              -- 'HLD-...'
  merchant_id   TEXT NOT NULL REFERENCES merchants(id),
  settlement_id TEXT REFERENCES settlements(id),
  hold_type     TEXT NOT NULL,                 -- RISK | KYC | BANK_ACCOUNT
  reason_code   TEXT NOT NULL,                 -- 'VELOCITY_SPIKE' | 'KYC_DOC_EXPIRED' | ...
  reason_text   TEXT NOT NULL,
  status        TEXT NOT NULL,                 -- ACTIVE | RELEASED
  placed_at     TEXT NOT NULL,
  released_at   TEXT
);

CREATE TABLE kyc_documents (
  id          INTEGER PRIMARY KEY,
  merchant_id TEXT NOT NULL REFERENCES merchants(id),
  doc_type    TEXT NOT NULL,                   -- PAN | AADHAAR | GSTIN | DRUG_LICENSE | SHOP_LICENSE
  status      TEXT NOT NULL,                   -- VALID | EXPIRED | PENDING | REJECTED
  verified_at TEXT,
  expires_at  TEXT
);

CREATE TABLE ledger_entries (                  -- every rupee that moved in or out of the merchant's wallet/bank
  id            INTEGER PRIMARY KEY,
  merchant_id   TEXT NOT NULL REFERENCES merchants(id),
  txn_ref       TEXT NOT NULL UNIQUE,          -- bank/NPCI style reference shown to the merchant
  entry_type    TEXT NOT NULL,  -- RENT_DEBIT | EDI_DEBIT | SETTLEMENT_CREDIT | REFUND_CREDIT | CHARGEBACK_DEBIT | BOOKING_DEBIT
  direction     TEXT NOT NULL,                 -- DEBIT | CREDIT
  amount_paise  INTEGER NOT NULL CHECK (amount_paise > 0),
  status        TEXT NOT NULL,                 -- SUCCESS | FAILED | REVERSED
  source_type   TEXT NOT NULL,                 -- subscription | loan | settlement | chargeback | action
  source_id     TEXT NOT NULL,                 -- id in the source table (actions.id as text for refunds)
  mandate_id    TEXT REFERENCES mandates(id),
  value_date    TEXT NOT NULL,                 -- business date of the movement
  posted_at     TEXT NOT NULL,
  narration     TEXT NOT NULL,                 -- what the merchant sees in the passbook
  refund_of     INTEGER UNIQUE REFERENCES ledger_entries(id)  -- DB-level double-refund guard
);

CREATE TABLE edi_attempts (                    -- every attempt to collect a loan instalment
  id              INTEGER PRIMARY KEY,
  loan_id         TEXT NOT NULL REFERENCES loans(id),
  due_date        TEXT NOT NULL,
  attempt_no      INTEGER NOT NULL,
  gateway_ref     TEXT NOT NULL UNIQUE,
  status          TEXT NOT NULL,               -- SUCCESS | FAILED | TIMEOUT  (TIMEOUT may still have debited!)
  responded_at    TEXT NOT NULL,
  ledger_entry_id INTEGER REFERENCES ledger_entries(id)
);

-- ───────────────────────────── travel: bus and flight tickets bought in the app ─────────────────────────────
-- A passenger is an account in `merchants` with category 'passenger'; the fare is a BOOKING_DEBIT ledger row,
-- so a fare refund is the same REFUND_CREDIT (refund_of = the fare row) as any other refund.

CREATE TABLE operators (                       -- bus operators and airlines (fictional)
  id    TEXT PRIMARY KEY,                      -- 'OP-VEGA'
  name  TEXT NOT NULL,
  kind  TEXT NOT NULL                          -- BUS | AIRLINE
);

CREATE TABLE trips (                           -- one bus run or one flight on one day
  id          TEXT PRIMARY KEY,                -- 'TR-VS2117-1109'
  operator_id TEXT NOT NULL REFERENCES operators(id),
  kind        TEXT NOT NULL,                   -- BUS | FLIGHT
  service_no  TEXT NOT NULL,                   -- 'VS-2117' | 'NB 615'
  origin      TEXT NOT NULL,
  destination TEXT NOT NULL,
  departs_at  TEXT NOT NULL,
  arrives_at  TEXT NOT NULL,                   -- scheduled arrival
  status      TEXT NOT NULL,                   -- SCHEDULED | DEPARTED | COMPLETED | CANCELLED  (set by the operator)
  status_at   TEXT
);

CREATE TABLE bookings (
  id              TEXT PRIMARY KEY,            -- 'PT-BUS-7731042' | 'PT-FLT-5520918'
  passenger_id    TEXT NOT NULL REFERENCES merchants(id),   -- the paying account (category 'passenger')
  trip_id         TEXT NOT NULL REFERENCES trips(id),
  passenger_name  TEXT NOT NULL,               -- name on the ticket
  seat            TEXT,                        -- bus berth, e.g. 'U7'
  pnr             TEXT,                        -- airline PNR
  pax             INTEGER NOT NULL DEFAULT 1,
  fare_paise      INTEGER NOT NULL CHECK (fare_paise > 0),
  status          TEXT NOT NULL,               -- CONFIRMED | CANCELLED
  booked_at       TEXT NOT NULL,
  boarded_at      TEXT,                        -- bus boarding scan; NULL = no scan
  cancelled_at    TEXT,
  ledger_entry_id INTEGER REFERENCES ledger_entries(id)   -- the fare debit
);

CREATE TABLE trip_events (                     -- what happened to the trip, from each source
  id         INTEGER PRIMARY KEY,
  trip_id    TEXT NOT NULL REFERENCES trips(id),
  source     TEXT NOT NULL,                    -- GPS | DRIVER | OPERATOR | AIRLINE
  event_type TEXT NOT NULL,                    -- DEPARTED | STOPPED | TURNED_BACK | RETURNED_TO_ORIGIN | ARRIVED | REPORT | CANCELLED
  at         TEXT NOT NULL,
  km         REAL,                             -- distance from the origin (GPS)
  note       TEXT
);

CREATE TABLE airline_refunds (                 -- money an airline sends back to Paytm for a cancelled flight
  id             TEXT PRIMARY KEY,             -- 'ARF-...'
  operator_id    TEXT NOT NULL REFERENCES operators(id),
  pnr            TEXT NOT NULL,
  passenger_name TEXT NOT NULL,
  amount_paise   INTEGER NOT NULL CHECK (amount_paise > 0),
  received_at    TEXT NOT NULL,
  booking_id     TEXT REFERENCES bookings(id), -- NULL until matched to a booking
  status         TEXT NOT NULL                 -- UNMATCHED | PASSED_ON
);

CREATE TABLE support_calls (                   -- helpline calls about a booking (summary written by the agent)
  id          TEXT PRIMARY KEY,
  merchant_id TEXT NOT NULL REFERENCES merchants(id),
  booking_id  TEXT REFERENCES bookings(id),
  at          TEXT NOT NULL,
  summary     TEXT NOT NULL
);

CREATE INDEX ix_bookings_trip      ON bookings(trip_id);
CREATE INDEX ix_bookings_passenger ON bookings(passenger_id);
CREATE INDEX ix_tripev_trip        ON trip_events(trip_id, at);
CREATE INDEX ix_airref_pnr         ON airline_refunds(pnr);

-- ───────────────────────────── Munim workflow tables ─────────────────────────────

CREATE TABLE cases (
  id             TEXT PRIMARY KEY,             -- 'CASE-0001'
  merchant_id    TEXT NOT NULL REFERENCES merchants(id),
  channel        TEXT NOT NULL,                -- chat | voice (team) | app (merchant app; money waits for a human)
  language       TEXT,                         -- detected language code
  complaint_text TEXT NOT NULL,
  scenario_id    TEXT,                         -- demo scenario key, NULL for free-typed complaints
  status         TEXT NOT NULL,  -- OPEN | INVESTIGATING | RESOLVED | AWAITING_APPROVAL | ESCALATED | BLOCKED | FAILED
  outcome        TEXT,                         -- Verdict.outcome
  verdict_json   TEXT,
  reply_text     TEXT,
  created_at     TEXT NOT NULL,
  closed_at      TEXT
);

CREATE TABLE sweeps (                          -- "serial offender" fleet sweeps
  id             TEXT PRIMARY KEY,             -- 'SWP-0001'
  source_case_id TEXT NOT NULL REFERENCES cases(id),
  suspect_id     TEXT NOT NULL,
  merchant_count INTEGER NOT NULL,
  total_paise    INTEGER NOT NULL,
  status         TEXT NOT NULL,                -- PENDING | APPROVED | REJECTED
  created_at     TEXT NOT NULL
);

CREATE TABLE actions (                         -- every action Munim proposed, with the firewall's decision
  id              INTEGER PRIMARY KEY,
  case_id         TEXT REFERENCES cases(id),
  sweep_id        TEXT REFERENCES sweeps(id),
  merchant_id     TEXT NOT NULL REFERENCES merchants(id),
  action_type     TEXT NOT NULL,               -- see models.ActionType
  amount_paise    INTEGER NOT NULL DEFAULT 0,
  params_json     TEXT NOT NULL,               -- final (post-firewall) parameters
  proposed_json   TEXT,                        -- what the LLM originally proposed
  firewall_json   TEXT,                        -- ActionVerdict for this action (checks + decision)
  verdict_json    TEXT,                        -- Verdict snapshot the action was judged against (re-used on approval)
  idempotency_key TEXT UNIQUE,                 -- NULL for BLOCKED rows
  decision        TEXT NOT NULL,               -- ALLOW | CORRECTED | NEEDS_HUMAN | BLOCK
  status          TEXT NOT NULL,               -- EXECUTED | PENDING_APPROVAL | REJECTED | BLOCKED | FAILED
  decided_by      TEXT,                        -- 'firewall' | 'human:<name>'
  created_at      TEXT NOT NULL,
  executed_at     TEXT
);

CREATE TABLE notifications (                   -- outbound messages (KYC links, pickup confirmations)
  id          INTEGER PRIMARY KEY,
  merchant_id TEXT NOT NULL REFERENCES merchants(id),
  case_id     TEXT REFERENCES cases(id),
  channel     TEXT NOT NULL,                   -- sms | whatsapp | app
  template    TEXT NOT NULL,
  body        TEXT NOT NULL,
  sent_at     TEXT NOT NULL
);

CREATE TABLE audit_log (
  id           INTEGER PRIMARY KEY,
  case_id      TEXT,
  actor        TEXT NOT NULL,                  -- llm | detective | firewall | executor | human | system
  event        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  at           TEXT NOT NULL
);

CREATE TABLE case_events (                     -- the live event stream of a case, persisted when the run ends (replay after restart)
  case_id   TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  type      TEXT NOT NULL,
  at        TEXT NOT NULL,
  data_json TEXT NOT NULL,
  PRIMARY KEY (case_id, seq)
);

CREATE INDEX ix_ledger_merchant   ON ledger_entries(merchant_id, value_date);
CREATE INDEX ix_ledger_source     ON ledger_entries(source_type, source_id);
CREATE INDEX ix_payments_device   ON payments(device_id, paid_at);
CREATE INDEX ix_payments_merchant ON payments(merchant_id, paid_at);
CREATE INDEX ix_devev_device      ON device_events(device_id, at);
CREATE INDEX ix_subs_merchant     ON subscriptions(merchant_id);
CREATE INDEX ix_actions_case      ON actions(case_id);
CREATE INDEX ix_audit_case        ON audit_log(case_id);
