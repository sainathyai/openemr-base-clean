"""Multimodal Evidence Agent — supervisor + two workers (Week 2).

           +-----------------------------------------+
   docs -> |               supervisor                | -> assemble -> END
 question  |  routes to whichever worker has work,   |
           |  loops back until the worklist is empty |
           +--------+---------------------+----------+
                    |                     |
             intake-extractor       evidence-retriever
             (parse->extract->      (hybrid RAG over the
              verify->facts)         guideline corpus)

The supervisor is a real router, not a fixed pipeline: it inspects the inputs,
decides which workers are needed (documents -> extractor; a clinical question or
extracted conditions -> retriever), dispatches one at a time, and reconvenes
after each. Extracted facts pass the extraction gate, so everything the agent
emits is grounded in either a document span or a cited guideline.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from .. import trace
from .extract import (
    Extractor,
    get_extractor,
    verify_intake_extraction,
    verify_lab_extraction,
)
from .pdf_parse import ParsedDocument
from .retriever import HybridRetriever, RetrievedPassage
from .schemas import ExtractionResult, to_context_facts


class EvidenceReport(BaseModel):
    correlation_id: str
    extraction: ExtractionResult
    evidence: list[RetrievedPassage] = []
    dropped: list[dict] = []
    query_used: Optional[str] = None
    stats: dict[str, int] = {}
    timings_ms: dict[str, int] = {}


class EvidenceState(TypedDict, total=False):
    docs: list[ParsedDocument]
    question: Optional[str]
    correlation_id: str
    # supervisor bookkeeping
    todo: list[str]
    done: list[str]
    # worker outputs
    extraction: ExtractionResult
    dropped: list[dict]
    evidence: list[RetrievedPassage]
    query_used: Optional[str]
    report: EvidenceReport
    timings_ms: dict[str, int]


def _now() -> float:
    return time.perf_counter()


def _plan(state: EvidenceState) -> list[str]:
    todo: list[str] = []
    if state.get("docs"):
        todo.append("extract")
    if state.get("question") or state.get("docs"):
        todo.append("retrieve")
    return todo


def _supervise(state: EvidenceState) -> EvidenceState:
    # first visit: build the worklist
    if "todo" not in state:
        return {"todo": _plan(state), "done": []}
    return {}  # nothing to change; routing happens on the conditional edge


def _route(state: EvidenceState) -> str:
    todo = state.get("todo", [])
    return todo[0] if todo else "assemble"


def _pop(state: EvidenceState, worker: str) -> dict:
    todo = [w for w in state.get("todo", []) if w != worker]
    done = list(state.get("done", [])) + [worker]
    return {"todo": todo, "done": done}


def _make_extract(extractor: Extractor):
    def _extract(state: EvidenceState) -> EvidenceState:
        t0 = _now()
        result = ExtractionResult()
        dropped: list[dict] = list(state.get("dropped", []))
        with trace.observe("intake-extractor", "span") as sp:
            for doc in state.get("docs", []):
                ext = extractor.extract(doc)
                if doc.doc_type == "lab_report":
                    kept, drp = verify_lab_extraction(doc, ext)
                    facts = to_context_facts(labs=kept)
                else:
                    kept, drp = verify_intake_extraction(doc, ext)
                    facts = to_context_facts(intake=kept)
                dropped.extend(drp)
                _merge(result, facts)
            trace.update(sp, output={
                "labs": len(result.lab_results),
                "meds": len(result.medications),
                "allergies": len(result.allergies),
                "problems": len(result.problems),
                "dropped": len(dropped),
            })
        # grounding rate = kept / (kept + dropped): the extraction accuracy signal
        kept_n = len(result.spans)
        total = kept_n + len(dropped)
        if total:
            trace.score("extraction_grounding_rate", kept_n / total,
                        comment=f"{kept_n}/{total} extracted facts grounded in the document")
        timings = dict(state.get("timings_ms", {}))
        timings["extract"] = int((_now() - t0) * 1000)
        out = _pop(state, "extract")
        out.update({"extraction": result, "dropped": dropped, "timings_ms": timings})
        return out
    return _extract


def _make_retrieve(retriever: HybridRetriever, k: int):
    def _retrieve(state: EvidenceState) -> EvidenceState:
        t0 = _now()
        query = state.get("question") or _query_from_facts(state.get("extraction"))
        with trace.observe("evidence-retriever", "span", input={"query": query}) as sp:
            hits = retriever.retrieve(query, k=k) if query else []
            trace.update(sp, output={"passages": len(hits),
                                     "ids": [h.id for h in hits]})
        timings = dict(state.get("timings_ms", {}))
        timings["retrieve"] = int((_now() - t0) * 1000)
        out = _pop(state, "retrieve")
        out.update({"evidence": hits, "query_used": query, "timings_ms": timings})
        return out
    return _retrieve


def _assemble(state: EvidenceState) -> EvidenceState:
    extraction = state.get("extraction") or ExtractionResult()
    dropped = state.get("dropped", [])
    evidence = state.get("evidence", [])
    report = EvidenceReport(
        correlation_id=state["correlation_id"],
        extraction=extraction,
        evidence=evidence,
        dropped=dropped,
        query_used=state.get("query_used"),
        stats={
            "labs": len(extraction.lab_results),
            "medications": len(extraction.medications),
            "allergies": len(extraction.allergies),
            "problems": len(extraction.problems),
            "dropped": len(dropped),
            "evidence": len(evidence),
        },
        timings_ms=state.get("timings_ms", {}),
    )
    return {"report": report}


def _merge(into: ExtractionResult, other: ExtractionResult) -> None:
    into.lab_results.extend(other.lab_results)
    into.medications.extend(other.medications)
    into.problems.extend(other.problems)
    into.allergies.extend(other.allergies)
    into.spans.update(other.spans)
    into.warnings.extend(other.warnings)


def _query_from_facts(extraction: Optional[ExtractionResult]) -> Optional[str]:
    """Build a retrieval query from extracted conditions/meds/abnormal-ish labs."""
    if extraction is None:
        return None
    terms: list[str] = []
    terms += [p.text for p in extraction.problems]
    terms += [m.text.split()[0] for m in extraction.medications if m.text]
    terms += [l.name for l in extraction.lab_results]
    query = " ".join(terms).strip()
    return query or None


def build_graph(extractor: Optional[Extractor] = None,
                retriever: Optional[HybridRetriever] = None,
                k: int = 4):
    extractor = extractor or get_extractor()
    retriever = retriever or HybridRetriever()
    g = StateGraph(EvidenceState)
    g.add_node("supervisor", _supervise)
    g.add_node("extract", _make_extract(extractor))
    g.add_node("retrieve", _make_retrieve(retriever, k))
    g.add_node("assemble", _assemble)
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", _route,
                            {"extract": "extract", "retrieve": "retrieve", "assemble": "assemble"})
    g.add_edge("extract", "supervisor")
    g.add_edge("retrieve", "supervisor")
    g.add_edge("assemble", END)
    return g.compile()


async def run(docs: Optional[list[ParsedDocument]] = None,
              question: Optional[str] = None,
              extractor: Optional[Extractor] = None,
              retriever: Optional[HybridRetriever] = None,
              k: int = 4) -> EvidenceReport:
    graph = build_graph(extractor, retriever, k)
    cid = uuid.uuid4().hex[:12]
    init: EvidenceState = {
        "docs": docs or [],
        "question": question,
        "correlation_id": cid,
        "timings_ms": {},
    }
    t0 = _now()
    with trace.observe("evidence_agent", "agent",
                       input={"docs": len(docs or []), "question": question},
                       metadata={"correlation_id": cid}) as root:
        out: EvidenceState = await graph.ainvoke(init)
        report = out["report"]
        trace.update(root, output=report.stats)
    report.timings_ms["wall"] = int((_now() - t0) * 1000)
    trace.flush()
    return report
