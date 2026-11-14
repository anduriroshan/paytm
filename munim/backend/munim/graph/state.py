"""LangGraph state for one investigation. Values are plain JSON dicts (model_dump(mode="json")) so the
state is serialisable and every node re-validates what it needs with the Pydantic models."""
from __future__ import annotations

from typing import Any, TypedDict


class CaseState(TypedDict, total=False):
    # input
    case_id: str
    merchant: dict[str, Any]          # {id, business_name, owner_name, city, language, category}
    complaint_text: str
    channel: str
    origin: str                       # "team" | "merchant" (opened in the merchant app; money always waits for a human)
    scenario_id: str | None
    today: str
    # detective work
    intake: dict[str, Any]            # IntakeResult
    hunch: dict[str, Any]             # Hunch
    trail: dict[str, Any]             # Trail
    sources: dict[str, Any]           # SourcesReport (backend/sources/: what was reached, and why)
    statements: list[dict[str, Any]]  # [StatementCheck]
    suspects: list[dict[str, Any]]    # [SuspectResult]
    probes: list[dict[str, Any]]      # [Probe]
    plan: dict[str, Any] | None       # final ProbePlan
    verdict: dict[str, Any]           # Verdict
    # resolution
    proposal: dict[str, Any]          # Proposal
    firewall: dict[str, Any]          # FirewallResult
    executions: list[dict[str, Any]]  # [ExecutionResult]
    reply: str
    language: str
    sweep: dict[str, Any] | None      # SweepResult
