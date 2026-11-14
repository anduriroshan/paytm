"""Ticket refunds for passenger accounts: bus and flight bookings paid through the app.

Two questions, answered from data only:
  bus     did the trip happen? The bus GPS and the driver's report are checked against the operator's trip status,
          which an operator can leave at DEPARTED even when the bus turned back.
  flight  did the airline already pay the fare back to us, and did it reach the passenger? An airline refund that
          arrives with only a PNR and a name can sit unmatched while the helpline keeps saying "waiting for the airline".

A passenger is an account in `merchants` with category 'passenger'. Each fare is a BOOKING_DEBIT ledger row
(source_type 'booking', source_id = booking id); a refund is a REFUND_CREDIT whose refund_of is that row.
Every function takes an open connection and only reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from munim.forensics import facts as F
from munim.models import EvidenceRef, rupees

PASS_ON_DAYS = 7          # money an airline paid back must reach the passenger within this many days

_BOOKING_SQL = """
SELECT b.*, t.kind AS trip_kind, t.service_no, t.origin, t.destination, t.departs_at, t.arrives_at,
       t.status AS trip_status, t.status_at AS trip_status_at, t.operator_id, o.name AS operator_name
FROM bookings b JOIN trips t ON t.id = b.trip_id JOIN operators o ON o.id = t.operator_id
WHERE b.passenger_id = ?"""

EVENT_WORD = {"DEPARTED": "Left", "STOPPED": "Stopped", "TURNED_BACK": "Turned back", "RETURNED_TO_ORIGIN": "Back at",
              "ARRIVED": "Arrived", "REPORT": "Report", "CANCELLED": "Cancelled"}


# ───────────────────────────── rows ─────────────────────────────

def bookings(conn, passenger_id: str, kind: str | None = None) -> list[dict]:
    sql = _BOOKING_SQL + (" AND t.kind = ?" if kind else "") + " ORDER BY b.booked_at, b.id"
    return F._rows(conn, sql, (passenger_id,) + ((kind,) if kind else ()))


def fare_debit(conn, passenger_id: str, booking_id: str) -> dict | None:
    """The fare debit of a booking, with its refund (refund_id …) if one was paid."""
    rows = F._rows(conn, F._DEBIT_SQL + " AND l.source_type = 'booking' AND l.source_id = ? ORDER BY l.id",
                   (passenger_id, "BOOKING_DEBIT", booking_id))
    return rows[0] if rows else None


def trip_events(conn, trip_id: str) -> list[dict]:
    return F._rows(conn, "SELECT * FROM trip_events WHERE trip_id = ? ORDER BY at, id", (trip_id,))


def airline_refund_for(conn, b: dict) -> dict | None:
    """What the airline paid back for this booking: matched by booking id, else by airline + PNR + fare + name."""
    r = F._one(conn, "SELECT * FROM airline_refunds WHERE booking_id = ? ORDER BY received_at, id LIMIT 1", (b["id"],))
    if r or not b.get("pnr"):
        return r
    return F._one(conn, """SELECT * FROM airline_refunds WHERE booking_id IS NULL AND operator_id = ? AND pnr = ?
                           AND amount_paise = ? AND UPPER(passenger_name) = UPPER(?) ORDER BY received_at, id LIMIT 1""",
                  (b["operator_id"], b["pnr"], b["fare_paise"], b["passenger_name"]))


def calls(conn, passenger_id: str, booking_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM support_calls WHERE merchant_id = ?" + (" AND booking_id = ?" if booking_id else "") + " ORDER BY at, id"
    return F._rows(conn, sql, (passenger_id,) + ((booking_id,) if booking_id else ()))


# ───────────────────────────── checks ─────────────────────────────

@dataclass
class BusCheck:
    booking: dict
    fare: dict | None
    events: list[dict]

    def event(self, etype: str, source: str | None = None) -> dict | None:
        return next((e for e in self.events if e["event_type"] == etype and (source is None or e["source"] == source)), None)

    @property
    def departed(self) -> dict | None:
        return self.event("DEPARTED")

    @property
    def stopped(self) -> dict | None:
        return self.event("STOPPED")

    @property
    def turned(self) -> dict | None:
        return self.event("TURNED_BACK")

    @property
    def back(self) -> dict | None:
        return self.event("RETURNED_TO_ORIGIN")

    @property
    def report(self) -> dict | None:
        return self.event("REPORT", "DRIVER")

    @property
    def arrived(self) -> dict | None:
        return self.event("ARRIVED")

    @property
    def km(self) -> float | None:
        kms = [e["km"] for e in (self.turned, self.stopped, self.report) if e and e["km"] is not None]
        return max(kms) if kms else None

    @property
    def boarded(self) -> bool:
        return bool(self.booking["boarded_at"])

    @property
    def confirmed(self) -> bool:
        return self.booking["status"] == "CONFIRMED"

    @property
    def failed(self) -> bool:
        """The bus came back to where it started and never arrived."""
        return self.back is not None and self.arrived is None

    @property
    def refunded(self) -> bool:
        return bool(self.fare and self.fare["refund_id"])

    @property
    def owed(self) -> bool:
        return bool(self.fare) and self.failed and self.boarded and self.confirmed and not self.refunded

    @property
    def no_show(self) -> bool:
        return bool(self.fare) and self.failed and not self.boarded and self.confirmed and not self.refunded


@dataclass
class FlightCheck:
    booking: dict
    fare: dict | None
    cancel: dict | None        # the airline's CANCELLED event
    paid: dict | None          # airline_refunds row: what the airline sent back to us
    today: date

    @property
    def cancelled(self) -> bool:
        return self.booking["trip_status"] == "CANCELLED"

    @property
    def refunded(self) -> bool:
        return bool(self.fare and self.fare["refund_id"])

    @property
    def days(self) -> int | None:
        d = F.as_date(self.paid["received_at"]) if self.paid else None
        return (self.today - d).days if d else None

    @property
    def matched(self) -> bool:
        return bool(self.paid and self.paid["booking_id"] == self.booking["id"])

    @property
    def open(self) -> bool:
        return bool(self.fare) and self.cancelled and not self.refunded

    @property
    def stuck(self) -> bool:
        return self.open and self.paid is not None and (self.days or 0) > PASS_ON_DAYS

    @property
    def in_window(self) -> bool:
        return self.open and self.paid is not None and (self.days or 0) <= PASS_ON_DAYS

    @property
    def not_paid(self) -> bool:
        return self.open and self.paid is None


def bus_checks(conn, passenger_id: str) -> list[BusCheck]:
    return [BusCheck(b, fare_debit(conn, passenger_id, b["id"]), trip_events(conn, b["trip_id"]))
            for b in bookings(conn, passenger_id, "BUS")]


def flight_checks(conn, passenger_id: str, today: Any) -> list[FlightCheck]:
    td = F.as_date(today) or date.today()
    out = []
    for b in bookings(conn, passenger_id, "FLIGHT"):
        cancel = next((e for e in trip_events(conn, b["trip_id"]) if e["event_type"] == "CANCELLED"), None)
        out.append(FlightCheck(b, fare_debit(conn, passenger_id, b["id"]), cancel, airline_refund_for(conn, b), td))
    return out


def wrongful(conn, passenger_id: str, today: Any = None, *, include_refunded: bool = True) -> dict[int, str]:
    """fare ledger id -> suspect id for fares that are owed back (include_refunded: also those already paid back)."""
    out: dict[int, str] = {}
    for c in bus_checks(conn, passenger_id):
        if c.fare and c.failed and c.boarded and c.confirmed and (include_refunded or not c.refunded):
            out[int(c.fare["id"])] = "BUS_TRIP_FAILED"
    for c in flight_checks(conn, passenger_id, today):
        if c.fare and c.cancelled and (c.stuck or (include_refunded and c.refunded)):
            out[int(c.fare["id"])] = "AIRLINE_REFUND_NOT_PASSED"
    return out


# ───────────────────────────── words ─────────────────────────────

def route(b: dict) -> str:
    return f"{b['origin']} → {b['destination']}"


def service(b: dict) -> str:
    """'bus VS-2117' / 'flight NB 615'."""
    return f"{'bus' if b['trip_kind'] == 'BUS' else 'flight'} {b['service_no']}"


def km(v: Any) -> str:
    return f"km {v:g}" if isinstance(v, (int, float)) else "km ?"


def event_label(e: dict, b: dict | None = None) -> str:
    when = f"{F.fday(e['at'])} {F.fhm(e['at'])}"
    if e["source"] == "DRIVER" and e["event_type"] == "REPORT":
        return f"Driver report · {when} · “{e['note']}”"
    if e["source"] == "AIRLINE":
        return f"Airline · {e['note'] or 'Cancelled'} · {when}"
    word = EVENT_WORD.get(e["event_type"], F.human_code(e["event_type"]))
    place = ""
    if b and e["event_type"] == "DEPARTED":
        place = f" {b['origin']}"
    elif b and e["event_type"] == "RETURNED_TO_ORIGIN":
        place = f" {b['origin']}"
    at_km = f" at {km(e['km'])}" if e["km"] and e["event_type"] in ("STOPPED", "TURNED_BACK") else ""
    return f"{e['source']} · {word}{place}{at_km} · {when}"


def booking_label(b: dict) -> str:
    if b["trip_kind"] == "BUS":
        seat = f" · berth {b['seat']}" if b["seat"] else ""
        scan = f" · boarded {F.fhm(b['boarded_at'])}" if b["boarded_at"] else " · no boarding scan"
        return (f"Bus ticket {b['id']} · {b['operator_name']} {b['service_no']} · {route(b)} · "
                f"{F.fday(b['departs_at'])} {F.fhm(b['departs_at'])}{seat}{scan}")
    return (f"Flight booking {b['id']} · {b['operator_name']} {b['service_no']} · {route(b)} · {F.fdate(b['departs_at'])} · "
            f"PNR {b['pnr']} · {b['pax']} pax · {rupees(b['fare_paise'])}")


def trip_label(b: dict) -> str:
    status = b["trip_status"]
    note = " (never marked cancelled)" if b["trip_kind"] == "BUS" and status in ("DEPARTED", "SCHEDULED") else ""
    return f"Trip {b['trip_id']} · {b['operator_name']} {b['service_no']} · operator status {status}{note}"


def airline_refund_label(r: dict, operator_name: str = "Airline") -> str:
    tail = "not matched to a booking" if not r["booking_id"] else f"matched to {r['booking_id']}"
    return (f"{operator_name} paid back {rupees(r['amount_paise'])} · {F.fdate(r['received_at'])} {F.fhm(r['received_at'])} · "
            f"{r['id']} · PNR {r['pnr']} · {tail}")


def call_label(c: dict) -> str:
    return f"Helpline call {F.fdate(c['at'])} {F.fhm(c['at'])} · {c['summary']}"


def booking_ref(b: dict) -> EvidenceRef:
    return EvidenceRef(table="bookings", row_id=b["id"], label=booking_label(b), at=b["boarded_at"] or b["booked_at"],
                       amount_paise=b["fare_paise"])


def trip_ref(b: dict) -> EvidenceRef:
    return EvidenceRef(table="trips", row_id=b["trip_id"], label=trip_label(b), at=b["trip_status_at"])


def event_ref(e: dict, b: dict | None = None) -> EvidenceRef:
    return EvidenceRef(table="trip_events", row_id=str(e["id"]), label=event_label(e, b), at=e["at"])


def airline_refund_ref(r: dict, operator_name: str = "Airline") -> EvidenceRef:
    return EvidenceRef(table="airline_refunds", row_id=r["id"], label=airline_refund_label(r, operator_name),
                       at=r["received_at"], amount_paise=r["amount_paise"])


def call_ref(c: dict) -> EvidenceRef:
    return EvidenceRef(table="support_calls", row_id=c["id"], label=call_label(c), at=c["at"])
