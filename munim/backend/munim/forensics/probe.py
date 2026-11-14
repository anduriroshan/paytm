"""Guarded read-only SQL for the improvise step. This is a security boundary: the LLM writes the SQL.

Layers (any one failing rejects the probe; nothing here ever raises):
  1. Lexical: one SELECT/WITH statement (a single trailing ';' at most), no write/DDL/PRAGMA/ATTACH/transaction keywords,
     no workflow or sqlite_* tables, must contain the merchant id as a quoted literal ('M1008') and no other merchant id.
  2. Isolation: the database file is opened read-only (URI mode=ro). The merchant's own business rows are copied into
     TEMP tables of the same names; the authorizer then allows READ only on those TEMP copies, so other merchants' rows and
     the workflow tables (cases, actions, audit_log, notifications, sweeps, case_events) are unreachable even through
     CTE aliases, schema-qualified names or subqueries.
  3. Engine: PRAGMA query_only, ATTACH limit 0, string/SQL length limits, an authorizer allowing only SELECT / READ (temp
     business tables) / FUNCTION (minus load_extension, randomblob, zeroblob, fts3_tokenizer) / RECURSIVE, a progress-handler
     time cap (TIME_LIMIT_S) and at most config.PROBE_ROW_LIMIT rows.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from munim.models import Probe

TIME_LIMIT_S = 2.0                     # module constant so tests can shorten it
MAX_SQL_CHARS = 4000
MAX_VALUE_BYTES = 1_000_000

BUSINESS_TABLES = ("merchants", "plans", "devices", "device_events", "pickup_tickets", "subscriptions", "loans", "mandates",
                   "settlements", "payments", "chargebacks", "holds", "kyc_documents", "ledger_entries", "edi_attempts",
                   "operators", "trips", "bookings", "trip_events", "airline_refunds", "support_calls")
WORKFLOW_TABLES = ("cases", "actions", "audit_log", "notifications", "sweeps", "case_events")
_BAD_FUNCS = {"load_extension", "randomblob", "zeroblob", "fts3_tokenizer", "readfile", "writefile", "edit"}

# rows of each business table that belong to the merchant (parameter: merchant id, repeated as needed)
_SCOPE = {
    "merchants": ("id = ?", 1),
    "plans": ("1", 0),
    "devices": ("merchant_id = ? OR id IN (SELECT device_id FROM main.subscriptions WHERE merchant_id = ?) "
                "OR id IN (SELECT device_id FROM main.pickup_tickets WHERE merchant_id = ?) "
                "OR id IN (SELECT device_id FROM main.payments WHERE merchant_id = ?)", 4),
    "device_events": ("device_id IN (SELECT id FROM temp.devices)", 0),
    "edi_attempts": ("loan_id IN (SELECT id FROM main.loans WHERE merchant_id = ?)", 1),
    "operators": ("1", 0),
    "bookings": ("passenger_id = ?", 1),
    "trips": ("id IN (SELECT trip_id FROM main.bookings WHERE passenger_id = ?)", 1),
    "trip_events": ("trip_id IN (SELECT trip_id FROM main.bookings WHERE passenger_id = ?)", 1),
    "airline_refunds": ("booking_id IN (SELECT id FROM main.bookings WHERE passenger_id = ?) "
                        "OR pnr IN (SELECT pnr FROM main.bookings WHERE passenger_id = ? AND pnr IS NOT NULL)", 2),
}
_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE\s+INTO|UPSERT|ATTACH|DETACH|PRAGMA|PRAGMA_\w+|CREATE|DROP|ALTER|VACUUM|"
                        r"REINDEX|ANALYZE|BEGIN|COMMIT|END\s+TRANSACTION|ROLLBACK|SAVEPOINT|RELEASE|LOAD_EXTENSION)\b", re.I)
_DENIED_TABLES = re.compile(r"\b(" + "|".join(WORKFLOW_TABLES) + r"|sqlite_\w*)\b", re.I)
_MERCHANT_ID = re.compile(r"(?<![A-Za-z0-9_])[Mm]\d{4,}(?!\d)")
_SAFE_MID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ProbeRejected(Exception):
    pass


# ───────────────────────────── layer 1: lexical ─────────────────────────────

def _scan(sql: str) -> tuple[str, list[str]]:
    """Split SQL into code (comments removed, string literals replaced by '?', quoted identifiers unquoted) and literals."""
    code: list[str] = []
    literals: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ProbeRejected("Unterminated string literal.")
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            literals.append("".join(buf))
            code.append(" ? ")
            i = j + 1
        elif ch in '"`[':
            close = {'"': '"', "`": "`", "[": "]"}[ch]
            j = sql.find(close, i + 1)
            if j < 0:
                raise ProbeRejected("Unterminated quoted identifier.")
            code.append(f" {sql[i + 1:j]} ")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            code.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise ProbeRejected("Unterminated comment.")
            i = j + 2
            code.append(" ")
        else:
            code.append(ch)
            i += 1
    return "".join(code), literals


def _strip_trailing_semicolon(sql: str) -> str:
    s = sql.rstrip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
        break
    return s


def check_sql(sql: Any, merchant_id: str) -> str:
    """Lexical guard. Returns the SQL to run (trailing ';' removed) or raises ProbeRejected with a friendly reason."""
    if not isinstance(sql, str) or not sql.strip():
        raise ProbeRejected("No SQL was given.")
    if len(sql) > MAX_SQL_CHARS:
        raise ProbeRejected(f"The query is too long (over {MAX_SQL_CHARS} characters).")
    if "\x00" in sql:
        raise ProbeRejected("The query contains a NUL character.")
    if not _SAFE_MID.match(merchant_id or ""):
        raise ProbeRejected("Invalid merchant id.")
    body = _strip_trailing_semicolon(sql)
    code, literals = _scan(body)
    if ";" in code:
        raise ProbeRejected("Only a single SELECT statement is allowed (found more than one statement).")
    first = re.match(r"\s*\(*\s*([A-Za-z]+)", code)
    if not first or first.group(1).upper() not in ("SELECT", "WITH"):
        raise ProbeRejected("Only a read-only SELECT (or WITH … SELECT) statement is allowed.")
    bad = _FORBIDDEN.search(code)
    if bad:
        raise ProbeRejected(f"'{bad.group(1).split()[0].upper()}' is not allowed: probes are read-only SELECTs.")
    denied = _DENIED_TABLES.search(code)
    if denied:
        raise ProbeRejected(f"Table '{denied.group(1)}' is not available to probes; only the business tables can be read.")
    others = sorted({m.group(0).upper() for m in _MERCHANT_ID.finditer(sql)} - {merchant_id.upper()})
    if others:
        raise ProbeRejected(f"The query mentions another merchant ({', '.join(others)}); probes may only look at {merchant_id}.")
    if merchant_id not in literals:
        raise ProbeRejected(f"The query must filter on this merchant: include the quoted literal '{merchant_id}' "
                            f"(e.g. WHERE merchant_id = '{merchant_id}').")
    return body


# ───────────────────────────── layer 2: isolation ─────────────────────────────

def _open_scoped(db_path: Path, merchant_id: str, allowed: frozenset[str] | None = None) -> sqlite3.Connection:
    """Copy the merchant's rows of the allowed business tables into TEMP tables; queries can read nothing else."""
    allowed = _ALLOWED_TABLES if allowed is None else allowed
    if not db_path.exists():
        raise ProbeRejected("The database is not available.")
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, check_same_thread=False)
    try:
        conn.execute("PRAGMA temp_store = MEMORY")
        ddl = dict(conn.execute("SELECT name, sql FROM main.sqlite_master WHERE type = 'table'").fetchall())
        for t in BUSINESS_TABLES:
            if t not in ddl or t not in allowed:
                continue
            where, nparams = _SCOPE.get(t, ("merchant_id = ?", 1))
            if t == "device_events" and "devices" not in allowed:      # no temp.devices to scope by
                dev_where, dev_n = _SCOPE["devices"]
                where, nparams = f"device_id IN (SELECT id FROM main.devices WHERE {dev_where})", dev_n
            create = re.sub(r"^\s*CREATE\s+TABLE\s+", "CREATE TEMP TABLE ", ddl[t] or "", count=1, flags=re.I)
            try:
                conn.execute(create)
            except sqlite3.Error:
                conn.execute(f"CREATE TEMP TABLE {t} AS SELECT * FROM main.{t} WHERE 0")
            conn.execute(f"INSERT INTO temp.{t} SELECT * FROM main.{t} WHERE {where}", (merchant_id,) * nparams)
        conn.execute("PRAGMA query_only = ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_CHARS + 16)
        conn.set_authorizer(_authorizer if allowed is _ALLOWED_TABLES else _make_authorizer(allowed))
        return conn
    except Exception:
        conn.close()
        raise


# ───────────────────────────── layer 3: engine ─────────────────────────────

_ALLOWED_TABLES = frozenset(BUSINESS_TABLES)


def _make_authorizer(allowed: frozenset[str]):
    def authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None) -> int:
        if action in (sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if dbname == "temp" and (arg1 or "").lower() in allowed:
                return sqlite3.SQLITE_OK
            if dbname is None:      # a CTE / subquery result; the reads inside its body are authorised separately
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _BAD_FUNCS else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorizer


_authorizer = _make_authorizer(_ALLOWED_TABLES)


def _check_connected(body: str, allowed: frozenset[str]) -> None:
    """Friendly lexical refusal for a business table whose source is turned off (the engine would deny it anyway)."""
    code, _ = _scan(body)
    for t in BUSINESS_TABLES:
        if t not in allowed and re.search(rf"(?<![\w.]){t}(?!\w)", code, re.I):
            raise ProbeRejected(f"Table '{t}' is not connected: its source is turned off in backend/sources/.")


def _cell(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


def _friendly(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "interrupted" in low:
        return f"Stopped by the {TIME_LIMIT_S:g}s time cap: the query ran too long."
    if "not authorized" in low or "prohibited" in low:
        return ("Blocked by the read-only guard: the query touched something outside this merchant's business tables "
                "(only plain SELECTs on the business tables are allowed).")
    if "readonly" in low or "read-only" in low or "query_only" in low:
        return "Blocked: the probe connection is read-only."
    if "too many attached" in low:
        return "Blocked: ATTACH is not allowed."
    return f"SQL error: {msg}"


def run_probe(sql: str, merchant_id: str, step: int, purpose: str, db_path=None,
              tables: frozenset[str] | set[str] | None = None) -> Probe:
    """Run one guarded, read-only, merchant-scoped SELECT. Never raises: failures come back as Probe(ok=False, error=...).
    tables: the business tables this probe may read (the connected database sources); None = all of them."""
    t0 = time.perf_counter()
    text = sql if isinstance(sql, str) else ("" if sql is None else str(sql))
    try:
        step_i = int(step)
    except (TypeError, ValueError):
        step_i = 0

    def done(ok: bool, columns=None, rows=None, error=None) -> Probe:
        return Probe(step=step_i, purpose=str(purpose or ""), sql=text, ok=ok, columns=columns or [], rows=rows or [], error=error,
                     ms=int((time.perf_counter() - t0) * 1000))

    conn = None
    try:
        from munim import config
        body = check_sql(text, str(merchant_id or ""))
        allowed = _ALLOWED_TABLES if tables is None else frozenset(tables) & _ALLOWED_TABLES
        if allowed is not _ALLOWED_TABLES:
            _check_connected(body, allowed)
        conn = _open_scoped(Path(db_path or config.DB_PATH), str(merchant_id), allowed)
        deadline = time.perf_counter() + float(TIME_LIMIT_S)
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 2000)
        cur = conn.execute(body)
        columns = [d[0] for d in (cur.description or [])]
        rows = [[_cell(v) for v in r] for r in cur.fetchmany(int(config.PROBE_ROW_LIMIT))]
        cur.close()
        return done(True, columns, rows)
    except ProbeRejected as e:
        return done(False, error=f"Rejected: {e}")
    except sqlite3.Warning as e:          # e.g. "You can only execute one statement at a time."
        return done(False, error=f"Rejected: {e}")
    except Exception as e:                # never raise to the graph
        return done(False, error=_friendly(e))
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
                conn.close()
            except Exception:
                pass
