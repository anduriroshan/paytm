"""The verdict: outcome, culprits and damages, decided by rules (never by LLM text).

  FAULT match with damages > 0  → REFUND_DUE      (HIGH; union of damages / targets / remedies; sweepable suspect)
  elif HOLD match               → HOLD_EXPLAINED  (HIGH)
  elif INNOCENT match           → NO_FAULT        (HIGH)
  elif plan.finding + evidence  → IMPROVISED      (LOW; [ESCALATE, NO_ACTION]; no damages)
  elif probes all failed, or the detective never reached a finding → UNSOLVED ([ESCALATE])
  else                          → NO_EVIDENCE     (MEDIUM; [NO_ACTION, ESCALATE])

Damages are the union of the culprits' damages_ledger_ids, priced from their ledger evidence rows.
"""
from __future__ import annotations

import re
from typing import Any

from munim.forensics import facts as F
from munim.models import (ActionType, ClaimKind, EvidenceRef, IntakeResult, Outcome, Probe, ProbePlan, SuspectResult,
                          Verdict, rupees)

A = ActionType
MAX_GUN = 12


def _as_suspect(s: Any) -> SuspectResult:
    return s if isinstance(s, SuspectResult) else SuspectResult.model_validate(s)


def _evidence(results: list[SuspectResult]) -> list[EvidenceRef]:
    seen, out = set(), []
    for s in results:
        for e in s.evidence:
            if (e.table, e.row_id) not in seen:
                seen.add((e.table, e.row_id))
                out.append(e)
    return out[:MAX_GUN]


def _union_targets(results: list[SuspectResult]) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for s in results:
        for k, v in s.targets.items():
            for part in str(v).split(","):
                part = part.strip()
                if part and part not in out.setdefault(k, []):
                    out[k].append(part)
    return {k: ",".join(v) for k, v in out.items() if v}


def _union_remedies(results: list[SuspectResult]) -> list[ActionType]:
    return list(dict.fromkeys(r for s in results for r in s.remedies))


def _damages(faults: list[SuspectResult]) -> tuple[list[int], int, dict[int, EvidenceRef]]:
    """Union of damage ids; each id priced once, from its ledger evidence (else an even share of the suspect's total)."""
    refs: dict[int, EvidenceRef] = {}
    for s in faults:
        for e in s.evidence:
            if e.table == "ledger_entries" and e.row_id.isdigit():
                refs.setdefault(int(e.row_id), e)
    amounts: dict[int, int] = {}
    for s in faults:
        ids = [i for i in dict.fromkeys(s.damages_ledger_ids)]
        known = {i: refs[i].amount_paise for i in ids if i in refs and refs[i].amount_paise}
        unknown = [i for i in ids if i not in known]
        rest = max(s.damages_paise - sum(known.values()), 0)
        for i, a in known.items():
            amounts.setdefault(i, int(a))
        for n, i in enumerate(unknown):
            share = rest // len(unknown) + (rest % len(unknown) if n == 0 else 0)
            amounts.setdefault(i, share)
    ids = list(amounts)
    return ids, sum(amounts.values()), refs


def _dates(ids: list[int], refs: dict[int, EvidenceRef]) -> str:
    return F.fdates(refs[i].at for i in ids if i in refs and refs[i].at)


def _refund_headline(faults: list[SuspectResult], ids: list[int], total: int, refs: dict[int, EvidenceRef],
                     targets: dict[str, str]) -> str:
    n = len(ids)
    when = _dates(ids, refs)
    when = f" ({when})" if when else ""
    if len(faults) > 1:
        titles = F.join_and([s.title.lower() for s in faults])
        return f"Refund due: {rupees(total)} for {F.plural(n, 'wrongful debit')}{when}: {titles}."
    s = faults[0]
    if s.suspect_id == "ZOMBIE_BILLING":
        dev = targets.get("device_id", "")
        what = f"Soundbox {dev}" if dev and "," not in dev else "Soundbox"
        return f"Refund due: {rupees(total)} for {F.plural(n, 'rent debit')} taken after the {what} was picked up{when}."
    if s.suspect_id == "DUPLICATE_SUBSCRIPTION":
        dup = [x for x in targets.get("subscription_id", "").split(",") if x]
        orig = next((e.row_id for e in s.evidence if e.table == "subscriptions" and e.row_id not in dup), None)
        dev = targets.get("device_id", "")
        same = f" the same Soundbox {dev}" if dev and "," not in dev else " the same Soundbox"
        dup_txt = F.join_and(dup) or "a duplicate subscription"
        vs = f" as {orig}" if orig else ""
        return (f"Refund due: {rupees(total)} for {F.plural(n, 'rent debit')} on duplicate subscription {dup_txt}, which bills{same}"
                f"{vs} a second time{when}.")
    if s.suspect_id == "DUPLICATE_EDI":
        loan = targets.get("loan_id", "")
        tail = ", after the gateway timed out but the bank still debited" if "timed out" in s.summary else ""
        what = "the duplicate EDI debit" if n == 1 else f"{F.plural(n, 'duplicate EDI debit')}"
        return f"Refund due: {rupees(total)} for {what} on loan {loan}{when}{tail}."
    return f"Refund due: {rupees(total)} for {F.plural(n, 'wrongful debit')}{when} ({s.title.lower()})."


def _label_part(label: str, idx: int) -> str:
    parts = label.split(" · ")
    return parts[idx] if len(parts) > idx else ""


def _hold_headline(h: SuspectResult) -> str:
    hold = next((e for e in h.evidence if e.table == "holds"), None)
    stls = [e for e in h.evidence if e.table == "settlements"]
    hid = h.targets.get("hold_id") or (hold.row_id if hold else "")
    total = sum(e.amount_paise or 0 for e in stls)
    if len(stls) == 1:
        what = f"settlement {stls[0].row_id} ({rupees(total)})"
    elif stls:
        what = f"{len(stls)} settlements ({rupees(total)})"
    else:
        what = "the merchant's payouts"
    if h.suspect_id == "RISK_HOLD":
        code = _label_part(hold.label, 1) if hold else ""
        code = f" ({code})" if code else ""
        return f"Payout held by risk: hold {hid}{code} is keeping back {what}; only the risk team can release it."
    if h.suspect_id == "KYC_HOLD":
        docs = []
        for e in h.evidence:
            if e.table == "kyc_documents":
                name, status = _label_part(e.label, 0).lower(), _label_part(e.label, 1)
                docs.append(f"{status.split(' ')[0].lower()} {name}" if status else name)
        why = f" until the {F.join_and(docs)} is updated" if docs else " until the KYC documents are updated"
        return f"Payout held for KYC: hold {hid} is keeping back {what}{why}."
    return f"Payout held: {F.lc_first(h.summary)}"


def _innocent_headline(s: SuspectResult) -> str:
    if s.suspect_id == "PICKUP_NOT_COMPLETED":
        dev = s.targets.get("device_id", "the Soundbox")
        req = re.search(r"requested on (\d{2} \w{3} \d{4})", s.summary)
        pays = re.search(r"taken (\d+ payments?) since", s.summary)
        since = f" since the {req.group(1)} return request" if req else ""
        used = f" and has taken {pays.group(1)}{since}" if pays else ""
        return (f"No refund due: {dev} was never picked up{used}, so the rent is legitimate; "
                f"a new pickup can be booked.")
    return f"No refund due: {F.lc_first(s.summary)}"


def _probe_rows(probes: list[Probe]) -> list[dict]:
    out = []
    for p in probes:
        if p.ok and p.columns:
            out += [dict(zip(p.columns, r)) for r in p.rows]
    return out


def _row_ledger_id(r: dict) -> int | None:
    for k in ("ledger_id", "ledger_entry_id", "id"):
        v = r.get(k)
        if v is not None and str(v).isdigit():
            return int(v)
    return None


def _probe_evidence(plan: ProbePlan, probes: list[Probe]) -> list[EvidenceRef]:
    rows = _probe_rows(probes)
    out: list[EvidenceRef] = []
    for lid in dict.fromkeys(plan.evidence_ledger_ids):
        r = next((r for r in rows if _row_ledger_id(r) == lid and (r.get("amount_paise") or r.get("entry_type"))), None) or \
            next((r for r in rows if _row_ledger_id(r) == lid), None)
        if r is None:
            out.append(EvidenceRef(table="ledger_entries", row_id=str(lid), label=f"Ledger entry #{lid}"))
            continue
        bits = []
        if r.get("amount_paise") is not None:
            kind = F._ENTRY.get(str(r.get("entry_type")), F.human_code(r.get("entry_type")) or "ledger entry")
            bits.append(f"{rupees(int(r['amount_paise']))} {kind}".strip())
        if r.get("value_date"):
            bits.append(F.fdate(r["value_date"]))
        if r.get("narration"):
            bits.append(str(r["narration"]))
        elif r.get("txn_ref"):
            bits.append(str(r["txn_ref"]))
        out.append(EvidenceRef(table="ledger_entries", row_id=str(lid), label=" · ".join(bits) or f"Ledger entry #{lid}",
                               at=r.get("posted_at") or r.get("value_date"),
                               amount_paise=int(r["amount_paise"]) if r.get("amount_paise") is not None else None))
    for r in rows:   # the settlement batch the probes examined, if any
        sid = r.get("id") or r.get("settlement_id")
        if isinstance(sid, str) and sid.startswith("STL-") and r.get("net_paise") is not None and len(out) < MAX_GUN:
            if all(e.row_id != sid for e in out):
                gross, ded = r.get("gross_paise"), r.get("deductions_paise")
                extra = f" (gross {rupees(gross)} − {rupees(ded)})" if gross is not None and ded is not None else ""
                out.append(EvidenceRef(table="settlements", row_id=sid,
                                       label=f"{sid} · {F.fdate(r.get('batch_date'))} · {r.get('status') or ''} · net {rupees(r['net_paise'])}{extra}"
                                       .replace(" ·  ·", " ·"),
                                       at=r.get("settled_at") or r.get("batch_date"), amount_paise=int(r["net_paise"])))
    return out[:MAX_GUN]


def _first_sentence(text: str, limit: int = 200) -> str:
    s = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _improvised_headline(plan: ProbePlan, gun: list[EvidenceRef], probes: list[Probe]) -> str:
    rows = _probe_rows(probes)
    cb = [r for r in rows if _row_ledger_id(r) in plan.evidence_ledger_ids and str(r.get("entry_type")) == "CHARGEBACK_DEBIT"]
    if cb:
        r = cb[0]
        ref = r.get("chargeback_id") or r.get("source_id")
        ref = f" ({ref})" if ref else ""
        when = f" on {F.fdate(r.get('value_date'))}" if r.get("value_date") else ""
        return (f"Likely explained: a {rupees(int(r['amount_paise']))} chargeback debit{ref}{when} accounts for the deduction; "
                f"not a system fault, needs human confirmation.")
    return f"Likely explained (needs human confirmation): {F.lc_first(_first_sentence(plan.finding or ''))}"


def decide_verdict(suspects: list[SuspectResult], intake: IntakeResult, plan: ProbePlan | None = None,
                   probes: list[Probe] | None = None) -> Verdict:
    suspects = [_as_suspect(s) for s in suspects or []]
    probes = [p if isinstance(p, Probe) else Probe.model_validate(p) for p in probes or []]
    match = [s for s in suspects if s.status == "MATCH"]

    faults = [s for s in match if s.kind == "FAULT" and s.damages_paise > 0 and s.damages_ledger_ids]
    if faults:
        ids, total, refs = _damages(faults)
        targets = _union_targets(faults)
        return Verdict(outcome=Outcome.REFUND_DUE, culprits=[s.suspect_id for s in faults],
                       headline=_refund_headline(faults, ids, total, refs, targets), smoking_gun=_evidence(faults),
                       damages_ledger_ids=ids, damages_paise=total, targets=targets, remedies=_union_remedies(faults),
                       confidence="HIGH", sweepable_suspect=next((s.suspect_id for s in faults if s.sweepable), None))

    holds = [s for s in match if s.kind == "HOLD"]
    if holds:
        return Verdict(outcome=Outcome.HOLD_EXPLAINED, culprits=[s.suspect_id for s in holds], headline=_hold_headline(holds[0]),
                       smoking_gun=_evidence(holds), targets=_union_targets(holds), remedies=_union_remedies(holds),
                       confidence="HIGH")

    innocent = [s for s in match if s.kind == "INNOCENT"]
    if innocent:
        return Verdict(outcome=Outcome.NO_FAULT, culprits=[s.suspect_id for s in innocent], headline=_innocent_headline(innocent[0]),
                       smoking_gun=_evidence(innocent), targets=_union_targets(innocent), remedies=_union_remedies(innocent),
                       confidence="HIGH")

    if plan is not None and (plan.finding or "").strip() and plan.evidence_ledger_ids:
        gun = _probe_evidence(plan, probes)
        return Verdict(outcome=Outcome.IMPROVISED, culprits=[], headline=_improvised_headline(plan, gun, probes),
                       smoking_gun=gun, remedies=[A.ESCALATE, A.NO_ACTION], confidence="LOW")

    failed = bool(probes) and all(not p.ok for p in probes)
    unfinished = plan is not None and (not plan.done or not (plan.finding or "").strip())
    if failed or unfinished:
        why = ("every read-only probe failed" if failed else "the probes did not reach a finding")
        return Verdict(outcome=Outcome.UNSOLVED, culprits=[],
                       headline=f"Unsolved: none of the {len(suspects) or 7} known causes matched and {why}; escalating with the case file.",
                       remedies=[A.ESCALATE], confidence="LOW")

    demand = next((c for c in F.claims_of(intake, ClaimKind.REFUND_DEMANDED) if c.amount_paise), None)
    if demand:
        head = f"No evidence: no debit, hold or deduction in the ledger supports the {rupees(demand.amount_paise)} refund demanded"
    else:
        head = "No evidence: none of the known causes matched and nothing in the ledger supports the complaint"
    if intake is not None and intake.injection_suspected:
        head += "; the message also tries to instruct the system, so nothing was actioned"
    return Verdict(outcome=Outcome.NO_EVIDENCE, culprits=[], headline=head + ".", remedies=[A.NO_ACTION, A.ESCALATE],
                   confidence="MEDIUM")
