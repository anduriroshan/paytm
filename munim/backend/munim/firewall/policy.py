"""Action Firewall: the LLM proposes, deterministic rules dispose.

`evaluate()` takes a Proposal (LLM output, treated as hostile) and a Verdict (computed from ledger rows) and
returns, per action, eleven checks in a fixed order plus a decision:

    FAIL → BLOCK    WARN → NEEDS_HUMAN    FIXED → CORRECTED    PASS / SKIP → nothing
    precedence BLOCK > NEEDS_HUMAN > CORRECTED > ALLOW; the aggregate is the worst across actions.

It only READS the database. Every `detail` is one human sentence (₹ and dates) shown verbatim in the UI.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from munim import config
from munim.db.conn import one, rows
from munim.models import (
    DECISION_RANK,
    ActionType,
    ActionVerdict,
    Decision,
    FirewallCheck,
    FirewallResult,
    Hunch,
    IntakeResult,
    Outcome,
    Proposal,
    ProposedAction,
    Verdict,
    rupees,
)

# ───────────────────────────── policy constants ─────────────────────────────

CHECK_ORDER: tuple[str, ...] = (
    "allowlist", "remedy_match", "hard_deny", "ownership", "evidence_binding", "amount_recompute",
    "idempotency", "preconditions", "limits", "injection", "confidence",
)

CHECK_LABELS: dict[str, str] = {
    "allowlist": "Known action type",
    "remedy_match": "Permitted remedy for the verdict",
    "hard_deny": "Hard-deny rules",
    "ownership": "Belongs to this merchant",
    "evidence_binding": "Bound to verdict evidence",
    "amount_recompute": "Amount recomputed from ledger",
    "idempotency": "Not already done",
    "preconditions": "Preconditions hold",
    "limits": "Within auto-approval limits",
    "injection": "Prompt-injection guard",
    "confidence": "Verdict confidence",
    # added outside evaluate(): by the sweep (WARN at queue time) and by approve()
    "sweep_batch": "Fleet-sweep sign-off",
    "human_approval": "Human approval",
}

STATUS_DECISION: dict[str, Decision] = {
    "FAIL": Decision.BLOCK,
    "WARN": Decision.NEEDS_HUMAN,
    "FIXED": Decision.CORRECTED,
    "PASS": Decision.ALLOW,
    "SKIP": Decision.ALLOW,
}

MONEY_ACTIONS = frozenset({ActionType.REFUND_DEBITS, ActionType.RELEASE_HOLD})
ALWAYS_PERMITTED = frozenset({ActionType.ESCALATE, ActionType.NO_ACTION})
ESCALATION_TEAMS: tuple[str, ...] = ("risk", "billing", "logistics", "payments", "lending")
REFUNDABLE_ENTRY_TYPES = frozenset({"RENT_DEBIT", "EDI_DEBIT"})
BAD_KYC_STATUSES = ("EXPIRED", "PENDING", "REJECTED")

MAX_ACTIONS = 8              # a proposal longer than this is itself suspicious
MAX_LEDGER_IDS = 200         # per action
MAX_TEXT = 500               # reason / target length cap


# ───────────────────────────── small helpers (also used by the executor) ─────────────────────────────

def fmt_date(value: str | None) -> str:
    """'2026-10-01' or '2026-10-01T06:00:00' → '01 Oct 2026'."""
    if not value:
        return "an unknown date"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return str(value)


def join_human(items: Iterable[Any], limit: int = 6) -> str:
    items = [str(i) for i in items]
    if len(items) > limit:
        items = items[:limit] + [f"{len(items) - limit} more"]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural_word or word + 's')}"


def ids_text(ids: Iterable[int]) -> str:
    return join_human([str(i) for i in ids], limit=8)


def describe_debits(ledger_rows: list[dict]) -> str:
    """'rent debits on 01 Oct 2026 and 01 Nov 2026' / 'the EDI debit on 12 Nov 2026'."""
    if not ledger_rows:
        return "no debits"
    kinds = {r.get("entry_type") for r in ledger_rows}
    noun = {"RENT_DEBIT": "rent debit", "EDI_DEBIT": "EDI debit"}.get(next(iter(kinds)), "debit") if len(kinds) == 1 else "debit"
    ordered = sorted(ledger_rows, key=lambda r: (r.get("value_date") or "", r.get("id") or 0))
    dates = join_human(fmt_date(r.get("value_date")) for r in ordered)
    if len(ordered) == 1:
        return f"the {noun} on {dates}"
    return f"{noun}s on {dates}"


def sentence_case(s: str) -> str:
    return s[:1].upper() + s[1:]


def team_name(team: str | None) -> str:
    return (team or "").strip().lower()


def idempotency_key(merchant_id: str, action: ProposedAction, case_id: str | None) -> str:
    """sha256(merchant|type|target|sorted ids)[:16]; ESCALATE / NO_ACTION also include the case id."""
    t = ActionType(action.action_type)
    target = team_name(action.team) if t == ActionType.ESCALATE else (action.target_id or "")
    ids = ",".join(str(i) for i in sorted(set(action.ledger_entry_ids)))
    parts = [merchant_id, t.value, target, ids]
    if t in ALWAYS_PERMITTED:
        parts.append(case_id or "")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def worst(decisions: Iterable[Decision]) -> Decision:
    out = Decision.ALLOW
    for d in decisions:
        if DECISION_RANK[d] > DECISION_RANK[out]:
            out = d
    return out


def decision_for(checks: Iterable[FirewallCheck]) -> Decision:
    return worst(STATUS_DECISION[c.status] for c in checks)


def make_check(check_id: str, status: str, detail: str) -> FirewallCheck:
    return FirewallCheck(id=check_id, label=CHECK_LABELS.get(check_id, check_id), status=status, detail=detail)


def held_settlements_for_hold(conn: sqlite3.Connection, hold: dict) -> list[dict]:
    """HELD settlements a hold covers: its own settlement plus the merchant's HELD batches since it was placed,
    excluding batches pinned by a different ACTIVE hold."""
    return rows(
        conn,
        """SELECT s.* FROM settlements s
           WHERE s.merchant_id = :m AND s.status = 'HELD'
             AND (s.id = :sid OR s.batch_date >= :placed)
             AND NOT EXISTS (SELECT 1 FROM holds h2 WHERE h2.settlement_id = s.id AND h2.id <> :hid AND h2.status = 'ACTIVE')
           ORDER BY s.batch_date, s.id""",
        {"m": hold["merchant_id"], "sid": hold.get("settlement_id") or "", "placed": (hold.get("placed_at") or "")[:10], "hid": hold["id"]},
    )


def auto_refunded_30d(conn: sqlite3.Connection, merchant_id: str, today: str) -> int:
    """Refund credits the firewall executed WITHOUT a human for this merchant in the 30 days up to `today` (business dates)."""
    r = one(
        conn,
        """SELECT COALESCE(SUM(le.amount_paise), 0) AS total
           FROM ledger_entries le JOIN actions a ON CAST(le.source_id AS INTEGER) = a.id
           WHERE le.merchant_id = ? AND le.entry_type = 'REFUND_CREDIT' AND le.source_type = 'action'
             AND le.value_date > date(?, '-30 days') AND le.value_date <= ?
             AND a.decided_by = 'firewall'""",
        (merchant_id, today, today),
    )
    return int(r["total"]) if r else 0


# ───────────────────────────── per-action working state ─────────────────────────────

@dataclass
class _Ctx:
    conn: sqlite3.Connection
    merchant_id: str
    merchant_name: str
    verdict: Verdict
    intake: IntakeResult | None
    hunch: Hunch | None
    case_id: str | None
    human_approved: bool
    today: str
    claimed_ids: dict[int, int] = field(default_factory=dict)      # ledger id -> action index (refunds in this proposal)
    claimed_keys: dict[str, int] = field(default_factory=dict)     # idempotency key -> action index
    batch_auto_paise: int = 0                                      # money already allowed earlier in this proposal
    _ledger: dict[int, dict | None] = field(default_factory=dict)
    _prior_30d: int | None = None

    def ledger(self, ids: Iterable[int]) -> dict[int, dict]:
        want = [i for i in dict.fromkeys(ids) if i not in self._ledger]
        for chunk in (want[i:i + 400] for i in range(0, len(want), 400)):
            found = {r["id"]: r for r in rows(self.conn, f"SELECT * FROM ledger_entries WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))}
            for i in chunk:
                self._ledger[i] = found.get(i)
        return {i: self._ledger[i] for i in ids if self._ledger.get(i) is not None}

    def prior_30d(self) -> int:
        if self._prior_30d is None:
            self._prior_30d = auto_refunded_30d(self.conn, self.merchant_id, self.today)
        return self._prior_30d

    @property
    def damages(self) -> list[int]:
        return list(dict.fromkeys(i for i in (self.verdict.damages_ledger_ids or []) if isinstance(i, int)))

    @property
    def injection(self) -> tuple[bool, str]:
        reasons = []
        if self.intake is not None and getattr(self.intake, "injection_suspected", False):
            reasons.append("intake flagged the complaint as trying to instruct the system")
        score = None
        if self.hunch is not None:
            try:
                score = float(self.hunch.injection_score)
            except (TypeError, ValueError):
                score = None
            if score is not None and not math.isnan(score) and score >= config.INJECTION_THRESHOLD:
                reasons.append(f"injection score {score:.2f} is at or above {config.INJECTION_THRESHOLD:.2f}")
        if reasons:
            return True, " and ".join(reasons)
        return False, (f"injection score {score:.2f}" if score is not None and not math.isnan(score) else "intake saw no manipulation")


@dataclass
class _Work:
    """The action as it is being corrected; becomes ActionVerdict.final."""
    raw_type: str
    type: ActionType | None
    amount: int
    proposed_amount: int
    ids: list[int]               # proposed ids (deduped, ints only)
    target: str | None
    team: str | None
    reason: str
    final_ids: list[int] | None = None     # set by evidence_binding for refunds
    evidence_failed: bool = False
    hold: dict | None = None
    held: list[dict] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)   # targets taken from the verdict (reported by ownership)

    @classmethod
    def from_proposed(cls, pa: Any) -> "_Work":
        raw = getattr(pa, "action_type", None)
        raw_type = str(getattr(raw, "value", raw))
        try:
            t: ActionType | None = ActionType(raw_type)
        except ValueError:
            t = None
        try:
            amount = int(getattr(pa, "amount_paise", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            amount = 0
        ids: list[int] = []
        for i in list(getattr(pa, "ledger_entry_ids", None) or []):
            if isinstance(i, bool):
                continue
            try:
                ids.append(int(i))
            except (TypeError, ValueError, OverflowError):
                continue
        ids = list(dict.fromkeys(ids))
        target = getattr(pa, "target_id", None)
        target = str(target).strip()[:MAX_TEXT] if target not in (None, "") else None
        team = getattr(pa, "team", None)
        team = str(team)[:64] if team not in (None, "") else None
        reason = str(getattr(pa, "reason", "") or "")[:MAX_TEXT]
        return cls(raw_type=raw_type, type=t, amount=amount, proposed_amount=amount, ids=ids, target=target or None, team=team, reason=reason)

    def to_action(self) -> ProposedAction:
        assert self.type is not None
        t = self.type
        ids = self.final_ids if t == ActionType.REFUND_DEBITS else self.ids
        return ProposedAction(
            action_type=t,
            amount_paise=max(0, self.amount),
            ledger_entry_ids=list(ids or []),
            target_id=None if t in (ActionType.REFUND_DEBITS, ActionType.NO_ACTION) else self.target,
            team=team_name(self.team) if t == ActionType.ESCALATE else None,
            reason=self.reason,
        )


# ───────────────────────────── the eleven checks ─────────────────────────────

def _remedy_match(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t = w.type
    v = ctx.verdict
    if t in ALWAYS_PERMITTED:
        return make_check("remedy_match", "PASS", f"{t.value} is always permitted, whatever the verdict.")
    permitted = list(dict.fromkeys([ActionType(r) for r in (v.remedies or [])] + [ActionType.ESCALATE, ActionType.NO_ACTION]))
    whom = join_human(v.culprits) if v.culprits else f"a {Outcome(v.outcome).value} verdict"
    if t in permitted:
        return make_check("remedy_match", "PASS", f"{t.value} is a permitted remedy for {whom}.")
    return make_check("remedy_match", "FAIL",
                      f"{t.value} is not a permitted remedy for {whom}; only {join_human(p.value for p in permitted)} are allowed.")


def _hard_deny(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t = w.type
    if t == ActionType.RELEASE_HOLD:
        hold = w.hold
        if hold and str(hold["hold_type"]).upper() == "RISK":
            return make_check("hard_deny", "FAIL",
                              f"Hold {hold['id']} is a RISK hold ({hold['reason_code']}); Munim never releases risk holds, even with human approval.")
        if hold:
            return make_check("hard_deny", "PASS", f"Hold {hold['id']} is a {hold['hold_type']} hold, not a RISK hold.")
        return make_check("hard_deny", "PASS", "No RISK hold is named, so the risk-hold hard deny does not trigger.")
    if t == ActionType.REFUND_DEBITS:
        outcome = Outcome(ctx.verdict.outcome)
        if outcome != Outcome.REFUND_DUE:
            return make_check("hard_deny", "FAIL",
                              f"Refunds need a REFUND_DUE verdict, but the evidence says {outcome.value}, so no money can move.")
        return make_check("hard_deny", "PASS", "The verdict is REFUND_DUE, so a refund is not hard-denied.")
    return make_check("hard_deny", "SKIP", f"No hard-deny rule covers {t.value}.")


def _owns_device(ctx: _Ctx, device_id: str) -> bool:
    return bool(one(
        ctx.conn,
        """SELECT 1 FROM devices d WHERE d.id = :d AND (d.merchant_id = :m
              OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.device_id = d.id AND s.merchant_id = :m)
              OR EXISTS (SELECT 1 FROM pickup_tickets p WHERE p.device_id = d.id AND p.merchant_id = :m))""",
        {"d": device_id, "m": ctx.merchant_id},
    ))


TARGET_KEYS: dict[ActionType, tuple[str, str]] = {
    ActionType.CANCEL_SUBSCRIPTION: ("subscription_id", "subscription id"),
    ActionType.SCHEDULE_PICKUP: ("device_id", "device id"),
    ActionType.RELEASE_HOLD: ("hold_id", "hold id"),
    ActionType.SEND_KYC_LINK: ("hold_id", "hold id"),
}


def _fill_target(ctx: _Ctx, w: _Work) -> None:
    """A missing target is taken from the verdict when it names exactly one (reported as FIXED by `ownership`)."""
    if w.type == ActionType.REFUND_DEBITS:
        w.target = None                      # refunds are defined by ledger ids only
        return
    spec = TARGET_KEYS.get(w.type)
    if not spec or w.target:
        return
    key, noun = spec
    cand = [x.strip() for x in str((ctx.verdict.targets or {}).get(key) or "").split(",") if x.strip()]
    if len(cand) == 1:
        w.target = cand[0]
        w.filled.append(f"no {noun} was proposed, so {cand[0]} was taken from the verdict")


def _ownership(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t, m, conn = w.type, ctx.merchant_id, ctx.conn
    problems: list[str] = []
    owned: list[str] = []
    plural_subject = False

    # ledger ids: the proposed ones, or (refunds with none proposed) the damages that will be used
    ids = list(w.ids)
    if not ids and t == ActionType.REFUND_DEBITS:
        ids = ctx.damages
    if len(ids) > MAX_LEDGER_IDS:
        problems.append(f"{len(ids)} ledger ids is more than the {MAX_LEDGER_IDS} one action may touch")
    elif ids:
        found = ctx.ledger(ids)
        missing = [i for i in ids if i not in found]
        foreign = [i for i in ids if i in found and found[i]["merchant_id"] != m]
        if missing:
            problems.append(f"ledger {'entry' if len(missing) == 1 else 'entries'} {ids_text(missing)} "
                            f"{'does' if len(missing) == 1 else 'do'} not exist")
        if foreign:
            problems.append(f"ledger {'entry' if len(foreign) == 1 else 'entries'} {ids_text(foreign)} "
                            f"{'belongs' if len(foreign) == 1 else 'belong'} to another merchant")
        if not missing and not foreign:
            owned.append(f"ledger {'entry' if len(ids) == 1 else 'entries'} {ids_text(ids)}")
            plural_subject = len(ids) > 1

    tgt = w.target
    if t == ActionType.CANCEL_SUBSCRIPTION:
        sub = one(conn, "SELECT merchant_id FROM subscriptions WHERE id = ?", (tgt,)) if tgt else None
        if not tgt:
            problems.append("no subscription id was given")
        elif not sub:
            problems.append(f"subscription {tgt} does not exist")
        elif sub["merchant_id"] != m:
            problems.append(f"subscription {tgt} belongs to another merchant")
        else:
            owned.append(f"subscription {tgt}")
    elif t == ActionType.SCHEDULE_PICKUP:
        if not tgt:
            problems.append("no device id was given")
        elif not one(conn, "SELECT 1 FROM devices WHERE id = ?", (tgt,)):
            problems.append(f"device {tgt} does not exist")
        elif not _owns_device(ctx, tgt):
            problems.append(f"device {tgt} belongs to another merchant")
        else:
            owned.append(f"device {tgt}")
    elif t == ActionType.RELEASE_HOLD:
        hold = w.hold
        if not tgt:
            problems.append("no hold id was given")
        elif not hold:
            problems.append(f"hold {tgt} does not exist")
        elif hold["merchant_id"] != m:
            problems.append(f"hold {tgt} belongs to another merchant")
        else:
            owned.append(f"hold {tgt}")
    elif t == ActionType.SEND_KYC_LINK and tgt:
        hold = one(conn, "SELECT merchant_id FROM holds WHERE id = ?", (tgt,))
        if hold:
            if hold["merchant_id"] != m:
                problems.append(f"hold {tgt} belongs to another merchant")
            else:
                owned.append(f"hold {tgt}")
        elif one(conn, "SELECT 1 FROM kyc_documents WHERE merchant_id = ? AND doc_type = ?", (m, tgt.upper())):
            w.target = tgt.upper()
            owned.append(f"the {w.target} document")
        else:
            problems.append(f"'{tgt[:40]}' is neither a hold nor a KYC document of this merchant")

    who = f"{ctx.merchant_name} ({m})"
    verb = "belong" if plural_subject or len(owned) > 1 else "belongs"
    if problems:
        return make_check("ownership", "FAIL", f"Rejected because {join_human(problems)}; Munim only touches {who}'s own records.")
    if w.filled:
        tail = f", and {join_human(owned)} {verb} to {who}" if owned else ""
        return make_check("ownership", "FIXED", sentence_case(f"{join_human(w.filled)}{tail}."))
    if not owned:
        return make_check("ownership", "SKIP", f"{t.value} names no ledger entry or record to check.")
    return make_check("ownership", "PASS", sentence_case(f"{join_human(owned)} {verb} to {who}."))


def _evidence_binding(ctx: _Ctx, w: _Work) -> FirewallCheck:
    if w.type != ActionType.REFUND_DEBITS:
        return make_check("evidence_binding", "SKIP", f"{w.type.value} is not a refund, so it is not bound to ledger damages.")
    damages = ctx.damages
    if not damages:
        w.final_ids, w.evidence_failed = [], True
        return make_check("evidence_binding", "FAIL",
                          "The verdict found no wrongly charged debits, so there is nothing this refund can be bound to.")
    dmg_rows = ctx.ledger(damages)
    if not w.ids:
        w.final_ids = damages
        return make_check("evidence_binding", "FIXED",
                          f"No ledger ids were proposed, so the refund was bound to the verdict's damages: "
                          f"{describe_debits(list(dmg_rows.values()))}.")
    kept = [i for i in w.ids if i in set(damages)]
    dropped = [i for i in w.ids if i not in set(damages)]
    if not kept:
        w.final_ids, w.evidence_failed = [], True
        return make_check("evidence_binding", "FAIL",
                          f"None of the proposed ledger ids ({ids_text(dropped)}) are among the debits the verdict found wrongly charged.")
    w.final_ids = kept
    kept_rows = list(ctx.ledger(kept).values())
    if dropped:
        return make_check("evidence_binding", "FIXED",
                          f"Dropped ledger {'id' if len(dropped) == 1 else 'ids'} {ids_text(dropped)} because the verdict does not "
                          f"name {'it' if len(dropped) == 1 else 'them'}; kept {describe_debits(kept_rows)}.")
    return make_check("evidence_binding", "PASS",
                      f"Every proposed ledger id is a damage named by the verdict: {describe_debits(kept_rows)}.")


def _amount_recompute(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t, proposed = w.type, w.amount
    if t == ActionType.REFUND_DEBITS:
        if not w.final_ids:
            w.amount = 0
            if proposed > 0:
                return make_check("amount_recompute", "FAIL",
                                  f"LLM proposed {rupees(proposed)} but no ledger rows back it, so the supported amount is ₹0.")
            return make_check("amount_recompute", "FAIL", "No ledger rows back this refund, so the supported amount is ₹0.")
        found = ctx.ledger(w.final_ids)
        lrows = [found[i] for i in w.final_ids if i in found]
        total = sum(int(r["amount_paise"]) for r in lrows)
        w.amount = total
        if proposed == total:
            return make_check("amount_recompute", "PASS",
                              f"{rupees(total)} matches the ledger: {describe_debits(lrows)}.")
        units = {int(r["amount_paise"]) for r in lrows}
        breakdown = ""
        if len(units) == 1 and proposed > 0:
            unit = next(iter(units))
            if unit and proposed % unit == 0 and proposed // unit > 1:
                breakdown = f" ({proposed // unit} × {rupees(unit)})"
        return make_check("amount_recompute", "FIXED",
                          f"LLM proposed {rupees(proposed)}{breakdown}; ledger supports {rupees(total)}: {describe_debits(lrows)}.")
    if t == ActionType.RELEASE_HOLD:
        if not w.hold or w.hold["merchant_id"] != ctx.merchant_id:
            w.amount = 0
            return make_check("amount_recompute", "SKIP", "There is no hold of this merchant to recompute an amount from.")
        held = held_settlements_for_hold(ctx.conn, w.hold)
        w.held = held
        total = sum(int(s["net_paise"]) for s in held)
        w.amount = total
        what = (f"{plural(len(held), 'held settlement')} ({join_human(fmt_date(s['batch_date']) for s in held)})"
                if held else "no held settlements")
        if proposed == total:
            return make_check("amount_recompute", "PASS", f"{rupees(total)} matches {what} under hold {w.hold['id']}.")
        return make_check("amount_recompute", "FIXED",
                          f"LLM proposed {rupees(proposed)}; hold {w.hold['id']} actually covers {rupees(total)} across {what}.")
    # non-money actions carry no amount
    w.amount = 0
    if proposed != 0:
        return make_check("amount_recompute", "FIXED", f"{t.value} moves no money, so the proposed {rupees(proposed)} was set to ₹0.")
    return make_check("amount_recompute", "SKIP", f"{t.value} moves no money.")


def _idempotency(ctx: _Ctx, w: _Work, idx: int, key: str) -> FirewallCheck:
    t = w.type
    if t == ActionType.REFUND_DEBITS:
        ids = w.final_ids or w.ids
        if ids:
            refunded = rows(
                ctx.conn,
                f"""SELECT r.refund_of, r.txn_ref, r.value_date, o.value_date AS orig_date, o.amount_paise AS orig_amount, o.entry_type
                    FROM ledger_entries r LEFT JOIN ledger_entries o ON o.id = r.refund_of
                    WHERE r.refund_of IN ({','.join('?' * len(ids))}) ORDER BY o.value_date""",
                tuple(ids),
            )
            if refunded:
                first = refunded[0]
                noun = {"RENT_DEBIT": "rent debit", "EDI_DEBIT": "EDI debit"}.get(first["entry_type"], "debit")
                more = f" (and {plural(len(refunded) - 1, 'other debit')})" if len(refunded) > 1 else ""
                return make_check("idempotency", "FAIL",
                                  f"The {rupees(first['orig_amount'])} {noun} of {fmt_date(first['orig_date'])}{more} was already refunded "
                                  f"on {fmt_date(first['value_date'])} ({first['txn_ref']}), so paying again would be a double refund.")
            overlap = [i for i in ids if i in ctx.claimed_ids]
            if overlap:
                return make_check("idempotency", "FAIL",
                                  f"Ledger {'id' if len(overlap) == 1 else 'ids'} {ids_text(overlap)} {'is' if len(overlap) == 1 else 'are'} "
                                  f"already being refunded by action {ctx.claimed_ids[overlap[0]] + 1} of this proposal, so this one is a duplicate.")
            if not ctx.human_approved:
                for p in rows(ctx.conn,
                              "SELECT id, params_json, sweep_id FROM actions WHERE merchant_id = ? AND action_type = 'REFUND_DEBITS' AND status = 'PENDING_APPROVAL'",
                              (ctx.merchant_id,)):
                    try:
                        pids = set(ProposedAction.model_validate_json(p["params_json"]).ledger_entry_ids)
                    except Exception:
                        continue
                    clash = [i for i in ids if i in pids]
                    if clash:
                        where = f" in sweep {p['sweep_id']}" if p["sweep_id"] else ""
                        return make_check("idempotency", "FAIL",
                                          f"Ledger {'id' if len(clash) == 1 else 'ids'} {ids_text(clash)} {'is' if len(clash) == 1 else 'are'} "
                                          f"already awaiting approval as action #{p['id']}{where}, so this would be a duplicate.")
    if key in ctx.claimed_keys:
        return make_check("idempotency", "FAIL", f"This is the same action as action {ctx.claimed_keys[key] + 1} of this proposal, so it was dropped as a duplicate.")
    statuses = ("EXECUTED",) if ctx.human_approved else ("EXECUTED", "PENDING_APPROVAL")
    prev = one(ctx.conn,
               f"SELECT id, status, case_id, sweep_id, executed_at, created_at FROM actions WHERE idempotency_key = ? AND status IN ({','.join('?' * len(statuses))})",
               (key, *statuses))
    if prev:
        where = prev["case_id"] or (f"sweep {prev['sweep_id']}" if prev["sweep_id"] else "an earlier run")
        if prev["status"] == "EXECUTED":
            return make_check("idempotency", "FAIL",
                              f"The identical action was already executed as action #{prev['id']} ({where}), so it will not run twice.")
        return make_check("idempotency", "FAIL",
                          f"The identical action is already awaiting approval as action #{prev['id']} ({where}), so it was not queued again.")
    if t == ActionType.REFUND_DEBITS:
        return make_check("idempotency", "PASS", "None of these debits has been refunded before and no identical refund exists.")
    return make_check("idempotency", "PASS", f"No identical {t.value} has been executed before.")


def _preconditions(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t, conn, m = w.type, ctx.conn, ctx.merchant_id
    if t == ActionType.REFUND_DEBITS:
        if not w.final_ids:
            return make_check("preconditions", "SKIP", "No debits are bound to this refund, so there is nothing to check.")
        found = ctx.ledger(w.final_ids)
        bad = [found[i] for i in w.final_ids if i in found and not (
            found[i]["direction"] == "DEBIT" and found[i]["status"] == "SUCCESS" and found[i]["entry_type"] in REFUNDABLE_ENTRY_TYPES)]
        if bad:
            b = bad[0]
            return make_check("preconditions", "FAIL",
                              f"Ledger entry {b['id']} is a {b['status']} {b['entry_type']} ({b['direction']}) of {fmt_date(b['value_date'])}, "
                              f"not a successful rent or EDI debit, so it cannot be refunded.")
        return make_check("preconditions", "PASS",
                          f"All {plural(len(found), 'entry', 'entries')} are successful debits that can be refunded.")
    if t == ActionType.CANCEL_SUBSCRIPTION:
        sub = one(conn, "SELECT * FROM subscriptions WHERE id = ? AND merchant_id = ?", (w.target, m)) if w.target else None
        if not sub:
            return make_check("preconditions", "FAIL", "There is no subscription of this merchant to cancel.")
        if sub["status"] != "ACTIVE":
            since = f" since {fmt_date(sub['ended_at'])}" if sub["ended_at"] else ""
            return make_check("preconditions", "FAIL", f"Subscription {sub['id']} is already {sub['status']}{since}, so there is nothing to cancel.")
        named = [x.strip() for x in str((ctx.verdict.targets or {}).get("subscription_id") or "").split(",") if x.strip()]
        if sub["id"] not in named:
            blamed = f"names {join_human(named)}" if named else "names no subscription"
            return make_check("preconditions", "FAIL", f"The verdict {blamed}, not {sub['id']}, so cancelling it is not justified.")
        return make_check("preconditions", "PASS",
                          f"Subscription {sub['id']} is ACTIVE (since {fmt_date(sub['started_at'])}) and is the one the verdict blames.")
    if t == ActionType.SCHEDULE_PICKUP:
        dev = one(conn, "SELECT * FROM devices WHERE id = ?", (w.target,)) if w.target else None
        if not dev:
            return make_check("preconditions", "FAIL", "There is no device to pick up.")
        if dev["status"] == "RETURNED":
            return make_check("preconditions", "FAIL", f"Device {dev['id']} is already RETURNED to the warehouse, so no pickup is needed.")
        open_t = one(conn,
                     "SELECT id, scheduled_for FROM pickup_tickets WHERE device_id = ? AND status IN ('OPEN','SCHEDULED') AND COALESCE(scheduled_for, ?) >= ? ORDER BY scheduled_for DESC",
                     (dev["id"], ctx.today, ctx.today))
        if open_t:
            return make_check("preconditions", "FAIL",
                              f"Pickup {open_t['id']} is already scheduled for {fmt_date(open_t['scheduled_for'])}, so another one is not needed.")
        return make_check("preconditions", "PASS", f"Device {dev['id']} is {dev['status']} and has no open pickup, so a pickup can be booked.")
    if t == ActionType.SEND_KYC_LINK:
        docs = rows(conn, "SELECT doc_type, status, expires_at FROM kyc_documents WHERE merchant_id = ? AND status IN ('EXPIRED','PENDING') ORDER BY doc_type", (m,))
        holds = rows(conn, "SELECT id, placed_at FROM holds WHERE merchant_id = ? AND hold_type = 'KYC' AND status = 'ACTIVE' ORDER BY placed_at", (m,))
        if not docs and not holds:
            return make_check("preconditions", "FAIL", "All KYC documents are valid and there is no active KYC hold, so no re-KYC link is needed.")
        bits = []
        for d in docs[:3]:
            when = f" (expired {fmt_date(d['expires_at'])})" if d["status"] == "EXPIRED" and d["expires_at"] else ""
            bits.append(f"{d['doc_type']} is {d['status']}{when}")
        for h in holds[:2]:
            bits.append(f"KYC hold {h['id']} has been active since {fmt_date(h['placed_at'])}")
        return make_check("preconditions", "PASS", f"A re-KYC link is needed: {join_human(bits)}.")
    if t == ActionType.RELEASE_HOLD:
        hold = w.hold
        if not hold or hold["merchant_id"] != m:
            return make_check("preconditions", "FAIL", "There is no hold of this merchant to release.")
        if hold["status"] != "ACTIVE":
            return make_check("preconditions", "FAIL", f"Hold {hold['id']} is already {hold['status']}, so there is nothing to release.")
        if str(hold["hold_type"]).upper() == "RISK":
            return make_check("preconditions", "FAIL", f"Hold {hold['id']} is a RISK hold, which only the risk team can release.")
        bad = rows(conn, f"SELECT doc_type, status, expires_at FROM kyc_documents WHERE merchant_id = ? AND status IN ({','.join('?' * len(BAD_KYC_STATUSES))}) ORDER BY doc_type",
                   (m, *BAD_KYC_STATUSES))
        if bad:
            d = bad[0]
            when = f" (expired {fmt_date(d['expires_at'])})" if d["status"] == "EXPIRED" and d["expires_at"] else ""
            return make_check("preconditions", "FAIL",
                              f"Hold {hold['id']} cannot be released while the {d['doc_type']} is {d['status']}{when}.")
        return make_check("preconditions", "PASS",
                          f"Hold {hold['id']} is an ACTIVE {hold['hold_type']} hold and every KYC document is VALID.")
    if t == ActionType.ESCALATE:
        team = team_name(w.team)
        if not team:
            return make_check("preconditions", "FAIL", f"No team was named; escalations go to {join_human(ESCALATION_TEAMS)}.")
        if team not in ESCALATION_TEAMS:
            return make_check("preconditions", "FAIL",
                              f"'{w.team}' is not a known team; escalations can only go to {join_human(ESCALATION_TEAMS)}.")
        return make_check("preconditions", "PASS", f"The {team} team is a valid escalation target.")
    return make_check("preconditions", "SKIP", f"{t.value} has no preconditions.")


def _limits(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t = w.type
    if t not in MONEY_ACTIONS:
        return make_check("limits", "SKIP", f"{t.value} moves no money.")
    # a refund that lost its evidence is judged on what the LLM asked for (it is BLOCKed anyway)
    amt = max(0, w.proposed_amount if (t == ActionType.REFUND_DEBITS and not w.final_ids) else w.amount)
    auto, cap = config.AUTO_LIMIT_PAISE, config.MERCHANT_30D_AUTO_LIMIT_PAISE
    total30 = ctx.prior_30d() + ctx.batch_auto_paise + amt
    if ctx.human_approved:
        side = "over" if (amt > auto or total30 > cap) else "within"
        return make_check("limits", "PASS", f"{rupees(amt)} is {side} the automatic limits and was approved by a human reviewer.")
    if amt > auto:
        return make_check("limits", "WARN", f"{rupees(amt)} is over the {rupees(auto)} auto-approval limit, so a human must sign off.")
    if total30 > cap:
        return make_check("limits", "WARN",
                          f"This would take the merchant's automatic payouts in the last 30 days to {rupees(total30)}, "
                          f"over the {rupees(cap)} cap, so a human must sign off.")
    return make_check("limits", "PASS",
                      f"{rupees(amt)} is within the {rupees(auto)} per-action limit, and the merchant's 30-day automatic total "
                      f"would be {rupees(total30)} of {rupees(cap)}.")


def _injection(ctx: _Ctx, w: _Work) -> FirewallCheck:
    flagged, why = ctx.injection
    t = w.type
    if not flagged:
        return make_check("injection", "PASS", f"No manipulation signals in the complaint ({why}).")
    if t not in MONEY_ACTIONS:
        return make_check("injection", "PASS", f"Injection suspected ({why}), but {t.value} moves no money.")
    if w.evidence_failed:
        return make_check("injection", "FAIL",
                          f"Injection suspected ({why}) and no ledger evidence backs this {rupees(max(w.proposed_amount, 0))} payout, so it is refused.")
    if ctx.human_approved:
        return make_check("injection", "PASS", f"Injection suspected ({why}), but a human reviewed the evidence and approved this payout.")
    return make_check("injection", "WARN",
                      f"Injection suspected ({why}), so this payout waits for a human even though the ledger backs it.")


def _confidence(ctx: _Ctx, w: _Work) -> FirewallCheck:
    t, conf = w.type, str(ctx.verdict.confidence)
    if t not in MONEY_ACTIONS:
        return make_check("confidence", "SKIP", f"{t.value} moves no money, so verdict confidence does not gate it.")
    if conf == "HIGH":
        return make_check("confidence", "PASS", "The verdict is HIGH confidence, computed from ledger rows.")
    if ctx.human_approved:
        return make_check("confidence", "PASS", f"The verdict is only {conf} confidence, but a human approved this payout.")
    return make_check("confidence", "WARN", f"The verdict is only {conf} confidence, and money moves automatically only on HIGH.")


# ───────────────────────────── orchestration ─────────────────────────────

def _evaluate_one(ctx: _Ctx, idx: int, pa: Any) -> ActionVerdict:
    w = _Work.from_proposed(pa)
    checks: list[FirewallCheck] = []

    def skip_rest(why: str) -> None:
        for cid in CHECK_ORDER[1:]:
            checks.append(make_check(cid, "SKIP", why))

    if idx >= MAX_ACTIONS:
        checks.append(make_check("allowlist", "FAIL",
                                 f"A proposal may hold at most {MAX_ACTIONS} actions; action {idx + 1} was refused unexamined."))
        skip_rest("Not evaluated because the action was refused by the allowlist.")
    elif w.type is None:
        checks.append(make_check("allowlist", "FAIL", f"'{w.raw_type[:40]}' is not an action Munim knows how to take."))
        skip_rest("Not evaluated because the action type is unknown.")
    else:
        _fill_target(ctx, w)
        if w.type == ActionType.RELEASE_HOLD and w.target:
            w.hold = one(ctx.conn, "SELECT * FROM holds WHERE id = ?", (w.target,))
        checks.append(make_check("allowlist", "PASS", f"{w.type.value} is on Munim's list of allowed actions."))
        checks.append(_remedy_match(ctx, w))
        checks.append(_hard_deny(ctx, w))
        checks.append(_ownership(ctx, w))
        checks.append(_evidence_binding(ctx, w))
        checks.append(_amount_recompute(ctx, w))
        key_action = w.to_action()
        if w.type == ActionType.REFUND_DEBITS and not w.final_ids:
            key_action.ledger_entry_ids = list(w.ids)
        checks.append(_idempotency(ctx, w, idx, idempotency_key(ctx.merchant_id, key_action, ctx.case_id)))
        checks.append(_preconditions(ctx, w))
        checks.append(_limits(ctx, w))
        checks.append(_injection(ctx, w))
        checks.append(_confidence(ctx, w))

    decision = decision_for(checks)
    if decision == Decision.BLOCK or w.type is None:
        return ActionVerdict(proposed=_safe_proposed(pa, w), final=None, decision=Decision.BLOCK, checks=checks, idempotency_key=None)
    final = w.to_action()
    key = idempotency_key(ctx.merchant_id, final, ctx.case_id)
    # remember what this proposal already claims, so later actions can't repeat it
    ctx.claimed_keys.setdefault(key, idx)
    if final.action_type == ActionType.REFUND_DEBITS:
        for i in final.ledger_entry_ids:
            ctx.claimed_ids.setdefault(i, idx)
    if final.action_type in MONEY_ACTIONS and decision in (Decision.ALLOW, Decision.CORRECTED):
        ctx.batch_auto_paise += final.amount_paise
    return ActionVerdict(proposed=_safe_proposed(pa, w), final=final, decision=decision, checks=checks, idempotency_key=key)


def _safe_proposed(pa: Any, w: _Work) -> ProposedAction:
    if isinstance(pa, ProposedAction) and w.type is not None:
        return pa
    # model_construct'd / garbage input: keep a valid echo of what was asked
    return ProposedAction.model_construct(
        action_type=w.raw_type, amount_paise=w.amount, ledger_entry_ids=w.ids, target_id=w.target, team=w.team, reason=w.reason)


def evaluate(conn: sqlite3.Connection, *, merchant_id: str, proposal: Proposal, verdict: Verdict,
             intake: IntakeResult | None = None, hunch: Hunch | None = None, case_id: str | None = None,
             human_approved: bool = False, today: str = config.DEMO_TODAY) -> FirewallResult:
    """Judge every proposed action. Pure apart from DB reads."""
    m = one(conn, "SELECT business_name FROM merchants WHERE id = ?", (merchant_id,))
    ctx = _Ctx(conn=conn, merchant_id=merchant_id, merchant_name=(m or {}).get("business_name") or "this merchant",
               verdict=verdict, intake=intake, hunch=hunch, case_id=case_id, human_approved=human_approved, today=today)
    actions = list(getattr(proposal, "actions", None) or [])
    out = [_evaluate_one(ctx, i, pa) for i, pa in enumerate(actions)]
    return FirewallResult(decision=worst(a.decision for a in out), actions=out)


__all__ = [
    "evaluate", "idempotency_key", "decision_for", "worst", "make_check", "fmt_date", "describe_debits", "rupees",
    "CHECK_ORDER", "CHECK_LABELS", "MONEY_ACTIONS", "ESCALATION_TEAMS", "held_settlements_for_hold", "auto_refunded_30d",
]
