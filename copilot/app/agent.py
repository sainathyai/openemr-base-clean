"""UC-1 pre-visit synthesis agent (LangGraph).

Graph:  prepare  ->  narrate  ->  verify  ->  render  ->  END

  prepare : pull the patient context (parallel FHIR) and compute the ChangeSet +
            fact allow-list. Fully deterministic.
  narrate : the swappable LLM step (Claude or stub) turns facts into structured
            claims. Untrusted output.
  verify  : the gate checks every claim against the ChangeSet. Fails CLOSED -
            unverifiable statements are dropped, never shown.
  render  : compose the final briefing, injecting values/refs from source data.

Every run carries a correlation_id and records per-node latency, token usage, and
verification pass/fail counts, so the whole thing is observable end to end.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from .changes import ChangeSet, compute_changes
from .fhir_client import FhirClient
from .llm import Narrator, get_narrator
from .summary import (
    Fact, SummaryDraft, VerifiedStatement, build_facts, render_summary, verify_draft,
)


class AgentState(TypedDict, total=False):
    patient_uuid: str
    correlation_id: str
    changeset: ChangeSet
    facts: dict[str, Fact]
    draft: SummaryDraft
    verified: list[VerifiedStatement]
    summary_md: str
    usage: dict[str, Any]
    timings_ms: dict[str, int]
    fetch_ms: dict[str, int]
    warnings: list[str]


def _now() -> float:
    return time.perf_counter()


async def _prepare(state: AgentState) -> AgentState:
    t0 = _now()
    client = FhirClient()
    try:
        ctx = await client.get_context(state["patient_uuid"])
    finally:
        await client.aclose()
    cs = compute_changes(ctx)
    facts = build_facts(cs)
    timings = dict(state.get("timings_ms", {}))
    timings["prepare"] = int((_now() - t0) * 1000)
    return {
        "changeset": cs, "facts": facts,
        "fetch_ms": ctx.fetch_ms,
        "warnings": list(ctx.warnings),
        "timings_ms": timings,
    }


def _make_narrate(narrator: Narrator):
    def _narrate(state: AgentState) -> AgentState:
        t0 = _now()
        draft, usage = narrator.narrate(state["changeset"], state["facts"])
        timings = dict(state.get("timings_ms", {}))
        timings["narrate"] = int((_now() - t0) * 1000)
        return {"draft": draft, "usage": usage, "timings_ms": timings}
    return _narrate


def _verify(state: AgentState) -> AgentState:
    t0 = _now()
    verified = verify_draft(state["draft"], state["facts"])
    timings = dict(state.get("timings_ms", {}))
    timings["verify"] = int((_now() - t0) * 1000)
    warnings = list(state.get("warnings", []))
    for vs in verified:
        if not vs.ok:
            warnings.append(
                f"dropped unverifiable claim ({vs.statement.category}): "
                + "; ".join(f"{x.rule}:{x.detail}" for x in vs.violations)
            )
    return {"verified": verified, "timings_ms": timings, "warnings": warnings}


def _render(state: AgentState) -> AgentState:
    md = render_summary(state["draft"].headline, state["verified"])
    return {"summary_md": md}


def build_graph(narrator: Optional[Narrator] = None):
    narrator = narrator or get_narrator()
    g = StateGraph(AgentState)
    g.add_node("prepare", _prepare)
    g.add_node("narrate", _make_narrate(narrator))
    g.add_node("verify", _verify)
    g.add_node("render", _render)
    g.set_entry_point("prepare")
    g.add_edge("prepare", "narrate")
    g.add_edge("narrate", "verify")
    g.add_edge("verify", "render")
    g.add_edge("render", END)
    return g.compile()


def verification_stats(verified: list[VerifiedStatement]) -> dict[str, int]:
    passed = sum(1 for v in verified if v.ok)
    return {"claims": len(verified), "passed": passed, "dropped": len(verified) - passed}


async def run(patient_uuid: str, narrator: Optional[Narrator] = None) -> AgentState:
    graph = build_graph(narrator)
    init: AgentState = {
        "patient_uuid": patient_uuid,
        "correlation_id": uuid.uuid4().hex[:12],
        "timings_ms": {}, "warnings": [],
    }
    t0 = _now()
    out: AgentState = await graph.ainvoke(init)
    out["timings_ms"]["wall"] = int((_now() - t0) * 1000)
    return out
