"""UC-3 conversational chart Q&A over one patient's context.

Multi-turn. The agent answers only from what the grounded tools (tools.py) return,
and must deliver its final answer through a structured `respond(answer, citations)`
tool so the grounding gate can check it:

  - every cited source_id must be one a tool actually returned this turn
  - every numeric value in the answer must appear in a returned fact

UC-1 (autonomous pre-visit synthesis) fails CLOSED and drops bad statements. UC-3
is interactive: the physician is reading in real time, so the agent still shows its
answer but flags it as ungrounded when a check fails, and records the violation.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import trace
from .schemas import PatientContext
from .summary import Violation
from .tools import TOOL_SCHEMAS, ContextTools

_NUM = re.compile(r"\d+(?:\.\d+)?")

_RESPOND_TOOL = {
    "name": "respond",
    "description": "Deliver the final answer to the physician. Call this exactly once, "
                   "after gathering what you need. Cite the source_id of every fact you use.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string",
                       "description": "Concise clinical answer. Only state values that a "
                                      "tool returned; cite their source_ids."},
            "citations": {"type": "array", "items": {"type": "string"},
                          "description": "source_ids the answer rests on."},
        },
        "required": ["answer", "citations"],
    },
}

_SYSTEM = """You are a clinical chart-assistant answering a primary-care physician's
questions about ONE patient during a visit. You can only know facts by calling the
provided tools; you have no other knowledge of this patient.

Rules:
- Never state a lab value, vital, medication, or diagnosis unless a tool returned it.
- Do not invent numbers. If a tool did not return a value, say you do not have it.
- Abnormality is decided by the tools (each lab carries a 'status' and reference
  range); report that status, do not reclassify it yourself.
- Answer concisely, then call `respond` with the answer and the source_ids you used.
- If the question cannot be answered from the chart, say so plainly."""


class ChatResult(BaseModel):
    question: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    grounded: bool = True
    violations: list[Violation] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


def check_grounding(answer: str, citations: list[str], tools: ContextTools) -> list[Violation]:
    v: list[Violation] = []
    for cid in citations:
        if cid not in tools.returned_ids:
            v.append(Violation(rule="ungrounded_citation",
                               detail=f"{cid} was not returned by any tool this turn"))
    for num in _NUM.findall(answer):
        if num not in tools.returned_numbers:
            v.append(Violation(rule="unsourced_number",
                               detail=f"value {num} not in any tool result"))
    return v


def _dispatch(tools: ContextTools, name: str, args: dict) -> Any:
    fn = getattr(tools, name, None)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:  # never crash the loop on a bad tool call
        return {"error": repr(e)}


class ClaudeChat:
    """Multi-turn tool-using agent backed by Claude."""

    def __init__(self, ctx: PatientContext, model: str | None = None, max_steps: int = 6) -> None:
        self.tools = ContextTools(ctx)
        self.model = model or os.getenv("COPILOT_MODEL", "claude-sonnet-5")
        self.name = f"claude:{self.model}"
        self.max_steps = max_steps
        self.messages: list[dict[str, Any]] = []

    def ask(self, question: str) -> ChatResult:
        import anthropic

        client = anthropic.Anthropic()
        # fresh grounding ledger per turn
        self.tools.reset()
        self.messages.append({"role": "user", "content": question})
        tool_calls: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        t0 = time.perf_counter()
        answer, citations = "", []

        with trace.observe("uc3_chart_qa", "agent", input={"question": question}) as root:
            for _ in range(self.max_steps):
                resp = client.messages.create(
                    model=self.model, max_tokens=1200, system=_SYSTEM,
                    tools=TOOL_SCHEMAS + [_RESPOND_TOOL], messages=self.messages,
                )
                usage["input_tokens"] += resp.usage.input_tokens
                usage["output_tokens"] += resp.usage.output_tokens
                self.messages.append({"role": "assistant", "content": resp.content})

                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if not tool_uses:
                    answer = "".join(b.text for b in resp.content if b.type == "text")
                    break

                responded = False
                results = []
                for tu in tool_uses:
                    if tu.name == "respond":
                        answer = tu.input.get("answer", "")
                        citations = tu.input.get("citations", [])
                        responded = True
                        results.append({"type": "tool_result", "tool_use_id": tu.id,
                                        "content": "delivered"})
                    else:
                        tool_calls.append(tu.name)
                        with trace.observe(tu.name, "tool", input=tu.input) as ts:
                            out = _dispatch(self.tools, tu.name, tu.input)
                            trace.update(ts, output=out)
                        results.append({"type": "tool_result", "tool_use_id": tu.id,
                                        "content": json.dumps(out, default=str)})
                self.messages.append({"role": "user", "content": results})
                if responded:
                    break

            violations = check_grounding(answer, citations, self.tools)
            trace.update(root, output={"answer": answer, "grounded": not violations},
                         usage_details={"input": usage["input_tokens"],
                                        "output": usage["output_tokens"]})
        trace.score("grounded", 1.0 if not violations else 0.0,
                    comment="; ".join(f"{v.rule}:{v.detail}" for v in violations) or "ok",
                    data_type="BOOLEAN")
        trace.flush()
        return ChatResult(
            question=question, answer=answer, citations=citations,
            grounded=len(violations) == 0, violations=violations,
            tool_calls=tool_calls,
            usage={"provider": self.name, **usage},
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )


class StubChat:
    """Offline keyword-routed agent (no API key). Exercises the tools + grounding
    gate deterministically for demos and CI."""

    name = "stub"

    def __init__(self, ctx: PatientContext) -> None:
        self.tools = ContextTools(ctx)

    def ask(self, question: str) -> ChatResult:
        self.tools.reset()
        q = question.lower()
        t0 = time.perf_counter()
        calls: list[str] = []
        answer, citations = "I do not have that in the chart.", []

        if any(w in q for w in ["chang", "new ", "since", "last visit"]):
            calls.append("whats_changed")
            cs = self.tools.whats_changed()
            parts = []
            for a in cs["current_abnormals"]:
                parts.append(f"{a['name'].split('[')[0].strip()} {a['status']} "
                             f"({a['value']} {a['unit']}, ref {a['reference']})")
            if cs["new_medications"]:
                parts.append(f"{len(cs['new_medications'])} new medication(s)")
            citations = ([a["source_id"] for a in cs["current_abnormals"]]
                         + [m["source_id"] for m in cs["new_medications"]])
            answer = ("Since the prior visit: " + "; ".join(parts) + ".") if parts \
                else "No notable changes since the prior visit."
        elif "allerg" in q:
            calls.append("list_allergies")
            al = self.tools.list_allergies()
            citations = [a["source_id"] for a in al]
            answer = ("Allergies: " + ", ".join(a["substance"] for a in al) + ".") if al \
                else "No documented allergies."
        elif any(w in q for w in ["med", "drug", "taking", "prescri", "thinner",
                                   "anticoag", "aspirin", "warfarin", "statin"]):
            calls.append("list_medications")
            meds = self.tools.list_medications()
            citations = [m["source_id"] for m in meds]
            answer = f"{len(meds)} medications on file: " + \
                     ", ".join(m["name"] for m in meds[:6]) + \
                     ("..." if len(meds) > 6 else ".")
        elif any(w in q for w in ["problem", "diagnos", "condition", "history"]):
            calls.append("list_problems")
            probs = self.tools.list_problems()
            citations = [p["source_id"] for p in probs[:8]]
            answer = f"{len(probs)} active problems, including: " + \
                     ", ".join(p["name"] for p in probs[:6]) + "."
        else:
            # treat the question as a lab name query
            term = _guess_lab_term(q)
            calls.append("find_labs")
            labs = [l for l in self.tools.find_labs(term) if l["value"] is not None]
            if labs:
                latest = labs[0]
                citations = [latest["source_id"]]
                ref = f", ref {latest['reference']}" if latest["reference"] else ""
                answer = (f"Latest {latest['name'].split('[')[0].strip()}: "
                          f"{latest['value']} {latest['unit'] or ''} "
                          f"({latest['status']}{ref}) on {latest['date']}.")
            else:
                answer = f"No lab results matching '{term}' with a recorded value in the chart."

        violations = check_grounding(answer, citations, self.tools)
        with trace.observe("uc3_chart_qa", "agent", input={"question": question},
                           metadata={"provider": "stub", "tools": calls}) as root:
            trace.update(root, output={"answer": answer, "grounded": not violations})
        trace.score("grounded", 1.0 if not violations else 0.0,
                    comment="; ".join(f"{v.rule}:{v.detail}" for v in violations) or "ok",
                    data_type="BOOLEAN")
        trace.flush()
        return ChatResult(
            question=question, answer=answer, citations=citations,
            grounded=len(violations) == 0, violations=violations,
            tool_calls=calls, usage={"provider": "stub"},
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )


_LAB_TERMS = ["creatinine", "potassium", "sodium", "glucose", "calcium", "chloride",
              "hemoglobin", "hematocrit", "cholesterol", "ldl", "hdl", "triglyceride",
              "platelet", "egfr", "bun", "albumin", "bilirubin", "wbc"]


def _guess_lab_term(q: str) -> str:
    for t in _LAB_TERMS:
        if t in q:
            return t
    return q.strip().rstrip("?").split()[-1] if q.strip() else ""


def get_chat(ctx: PatientContext):
    from .config import settings
    if not settings.force_stub and os.getenv("ANTHROPIC_API_KEY"):
        return ClaudeChat(ctx)
    return StubChat(ctx)
