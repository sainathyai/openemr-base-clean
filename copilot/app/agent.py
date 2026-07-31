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

from . import trace
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
    with trace.observe("prepare", "span") as sp:
        client = FhirClient()
        try:
            ctx = await client.get_context(state["patient_uuid"])
        finally:
            await client.aclose()
        cs = compute_changes(ctx)
        facts = build_facts(cs)
        trace.update(sp, output={"facts": len(facts), "abnormals": len(cs.current_abnormals),
                                 "new_meds": len(cs.new_medications)},
                     metadata={"fetch_ms": ctx.fetch_ms})
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
        with trace.observe("narrate", "generation", model=getattr(narrator, "name", "?"),
                           input={"facts": len(state["facts"])}) as gen:
            draft, usage = narrator.narrate(state["changeset"], state["facts"])
            trace.update(gen, output={"statements": len(draft.statements)},
                         usage_details={"input": usage.get("input_tokens", 0),
                                        "output": usage.get("output_tokens", 0)})
        timings = dict(state.get("timings_ms", {}))
        timings["narrate"] = int((_now() - t0) * 1000)
        return {"draft": draft, "usage": usage, "timings_ms": timings}
    return _narrate


def _verify(state: AgentState) -> AgentState:
    t0 = _now()
    with trace.observe("verify", "guardrail") as gd:
        verified = verify_draft(state["draft"], state["facts"])
        passed = sum(1 for v in verified if v.ok)
        trace.update(gd, output={"passed": passed, "dropped": len(verified) - passed})
    if verified:
        trace.score("verification_pass_rate", passed / len(verified),
                    comment=f"{passed}/{len(verified)} claims verified")
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
    cid = uuid.uuid4().hex[:12]
    init: AgentState = {
        "patient_uuid": patient_uuid,
        "correlation_id": cid,
        "timings_ms": {}, "warnings": [],
    }
    t0 = _now()
    with trace.observe("uc1_previsit_synthesis", "agent",
                       input={"patient_uuid": patient_uuid},
                       metadata={"correlation_id": cid}) as root:
        out: AgentState = await graph.ainvoke(init)
        trace.update(root, output={"summary_chars": len(out.get("summary_md", ""))})
    out["timings_ms"]["wall"] = int((_now() - t0) * 1000)
    trace.flush()
    return out
