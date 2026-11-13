"""The investigation graph.

    START ─┬─ intake (LLM) ──┐
           └─ hunch (Prism) ─┴─ trail ─ statements ─ lineup ─┬──────────────┬─ verdict ─ propose (LLM)
                                                              └─ improvise ──┘   (only if no suspect matched)
    ─ firewall (code) ─ execute (code) ─ respond (LLM) ─┬─ sweep ─ END
                                                         └─────────── END   (only if a fleet-wide fault was refunded/queued)
"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import CaseState


def build_graph():
    g = StateGraph(CaseState)
    for name in ("intake", "hunch", "trail", "statements", "lineup", "improvise", "verdict",
                 "propose", "firewall", "execute", "respond", "sweep"):
        g.add_node(name, getattr(nodes, name))

    # intake (LLM) and hunch (Prism) run in parallel; the trail waits for both
    g.add_edge(START, "intake")
    g.add_edge(START, "hunch")
    g.add_edge(["intake", "hunch"], "trail")
    g.add_edge("trail", "statements")
    g.add_edge("statements", "lineup")
    g.add_conditional_edges("lineup", lambda s: "verdict" if nodes.any_suspect_matched(s) else "improvise",
                            {"verdict": "verdict", "improvise": "improvise"})
    g.add_edge("improvise", "verdict")
    g.add_edge("verdict", "propose")
    g.add_edge("propose", "firewall")
    g.add_edge("firewall", "execute")
    g.add_edge("execute", "respond")
    g.add_conditional_edges("respond", lambda s: "sweep" if nodes.should_sweep(s) else END,
                            {"sweep": "sweep", END: END})
    g.add_edge("sweep", END)
    return g.compile()


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()
