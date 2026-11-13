"""Live event contract streamed to the UI over SSE (GET /api/cases/{id}/events).

Every event: {"seq": int, "case_id": str, "type": EventType, "at": iso, "data": {...}}
The frontend mirrors these in frontend/src/types.ts. Payloads are model_dump()s of munim.models types.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class EventType(StrEnum):
    CASE = "case"                # {case_id, merchant:{id,business_name,owner_name,city,language,category}, complaint_text, channel, scenario_id}
    NODE = "node"                # {node, agent, status:'start'|'end'|'skip'|'error', ms?, summary?}
    LLM = "llm"                  # {node, role:'reasoner'|'writer', provider, model, kind:'content'|'reasoning', delta}
    LLM_DONE = "llm_done"        # {node, role, provider, model, ms, text, usage?, live:bool}
    INTAKE = "intake"            # IntakeResult
    HUNCH = "hunch"              # Hunch
    TRAIL = "trail"              # Trail
    STATEMENTS = "statements"    # {checks: [StatementCheck]}
    SUSPECT = "suspect"          # SuspectResult (one event per suspect, in test order)
    PROBE = "probe"              # Probe (improvise only)
    VERDICT = "verdict"          # Verdict
    PROPOSAL = "proposal"        # Proposal
    FIREWALL = "firewall"        # FirewallResult
    EXECUTION = "execution"      # {results: [ExecutionResult]}
    MESSAGE = "message"          # {role:'merchant'|'munim', text, language, final:bool, en?, kind?:'chat', sources?, new_case?}
    SWEEP = "sweep"              # SweepResult
    APPROVAL = "approval"        # {action_id, status:'EXECUTED'|'REJECTED'|..., result: ExecutionResult, approver}
    ERROR = "error"              # {node, message}
    DONE = "done"                # {case_id, status, outcome, decision, ms}


# Graph nodes, the "agent" each belongs to (for the UI swimlanes), and display order.
NODES: list[tuple[str, str, str]] = [
    # (node, agent, label)
    ("intake", "Intake Agent", "Understand complaint"),
    ("hunch", "Detective", "Hunch (Sarvam Prism)"),
    ("trail", "Detective", "Follow the money trail"),
    ("statements", "Detective", "Check merchant's statements"),
    ("lineup", "Detective", "Suspect lineup"),
    ("improvise", "Detective", "Improvise probes (read-only SQL)"),
    ("verdict", "Detective", "Verdict"),
    ("propose", "Resolver Agent", "Propose action"),
    ("firewall", "Action Firewall", "Firewall (deterministic)"),
    ("execute", "Executor", "Execute / queue for approval"),
    ("respond", "Communicator Agent", "Reply to merchant"),
    ("sweep", "Detective", "Serial-offender sweep"),
]


class Event(BaseModel):
    seq: int
    case_id: str
    type: EventType
    at: str
    data: dict[str, Any]


def make_event(seq: int, case_id: str, type_: EventType | str, data: Any) -> Event:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return Event(seq=seq, case_id=case_id, type=EventType(type_), at=datetime.now().isoformat(timespec="milliseconds"), data=data)
