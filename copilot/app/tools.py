"""Grounded retrieval tools over one patient's already-fetched context (UC-3).

The conversational agent may only learn facts through these tools. Each returns
typed, source-attributed rows drawn from the in-memory PatientContext (no new
fetch, no free text), and records which source_ids and values it surfaced. That
record is what the grounding gate later checks the answer against, so the model
cannot cite or quote anything a tool did not actually return.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .changes import compute_changes
from .reference_ranges import classify_all
from .schemas import PatientContext

_MIN = datetime.min.replace(tzinfo=timezone.utc)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _aware(dt) -> datetime:
    if dt is None:
        return _MIN
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ContextTools:
    def __init__(self, ctx: PatientContext) -> None:
        self.ctx = ctx
        self.assessments = classify_all(ctx.labs, ctx.demographics)
        self.returned_ids: set[str] = set()
        self.returned_renders: list[str] = []
        self.returned_numbers: set[str] = set()  # every number the gate will allow

    def reset(self) -> None:
        self.returned_ids.clear()
        self.returned_renders.clear()
        self.returned_numbers.clear()

    # -- bookkeeping so the grounding gate knows what was actually surfaced --
    def _record(self, source_id: str, render: str) -> None:
        self.returned_ids.add(source_id)
        self.returned_renders.append(render)
        self.returned_numbers.update(_NUM.findall(render))

    def _count(self, n: int) -> None:
        # the cardinality of a result set is itself a grounded fact to state
        self.returned_numbers.add(str(n))

    # -- the tools --

    def find_labs(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Most recent lab results whose name or LOINC matches `query`
        (case-insensitive substring), each with its deterministic abnormality
        verdict and reference range."""
        q = (query or "").lower().strip()
        hits = [a for a in self.assessments
                if q in a.name.lower() or (a.loinc and q in a.loinc.lower())]
        hits.sort(key=lambda a: _aware(a.effective), reverse=True)
        out = []
        for a in hits[:limit]:
            val = round(a.numeric_value, 2) if a.numeric_value is not None else a.value
            render = (f"{a.name} = {val} {a.unit or ''} status={a.status} "
                      f"ref={a.reference_display or 'n/a'} "
                      f"date={a.effective.date() if a.effective else '?'}")
            self._record(a.source_id, render)
            out.append({
                "source_id": a.source_id, "name": a.name, "loinc": a.loinc,
                "value": val, "unit": a.unit, "status": a.status,
                "reference": a.reference_display, "provenance": a.provenance,
                "date": str(a.effective.date()) if a.effective else None,
            })
        self._count(len(out))
        return out

    def list_medications(self) -> list[dict[str, Any]]:
        out = []
        for m in self.ctx.medications:
            self._record(m.source_id, f"medication: {m.text} status={m.status}")
            out.append({"source_id": m.source_id, "name": m.text,
                        "status": m.status,
                        "authored_on": str(m.authored_on) if m.authored_on else None})
        self._count(len(out))
        return out

    def list_problems(self, active_only: bool = True) -> list[dict[str, Any]]:
        out = []
        for p in self.ctx.problems:
            active = (p.clinical_status or "active").lower() == "active"
            if active_only and not active:
                continue
            self._record(p.source_id, f"problem: {p.text} status={p.clinical_status}")
            out.append({"source_id": p.source_id, "name": p.text,
                        "clinical_status": p.clinical_status,
                        "onset": str(p.onset) if p.onset else None})
        self._count(len(out))
        return out

    def list_allergies(self) -> list[dict[str, Any]]:
        out = []
        for a in self.ctx.allergies:
            self._record(a.source_id, f"allergy: {a.text} criticality={a.criticality}")
            out.append({"source_id": a.source_id, "substance": a.text,
                        "criticality": a.criticality})
        self._count(len(out))
        return out

    def latest_vitals(self, limit: int = 8) -> list[dict[str, Any]]:
        vs = sorted(self.ctx.vitals, key=lambda v: _aware(v.effective), reverse=True)
        out = []
        for v in vs[:limit]:
            self._record(v.source_id, f"vital: {v.name} = {v.value} {v.unit or ''}")
            out.append({"source_id": v.source_id, "name": v.name,
                        "value": v.value, "unit": v.unit,
                        "date": str(v.effective.date()) if v.effective else None})
        self._count(len(out))
        return out

    def whats_changed(self) -> dict[str, Any]:
        """The deterministic UC-1 change-set (new problems/meds, abnormal and
        worsening labs since the prior visit)."""
        cs = compute_changes(self.ctx)
        for a in cs.current_abnormals:
            val = round(a.numeric_value, 2) if a.numeric_value is not None else a.value
            self._record(a.source_id, f"{a.name} = {val} {a.unit or ''} status={a.status} "
                                      f"ref={a.reference_display or 'n/a'}")
        for m in cs.new_medications:
            self._record(m.source_id, f"new medication: {m.text}")
        for p in cs.new_problems:
            self._record(p.source_id, f"new problem: {p.text}")
        self._count(len(cs.new_medications))
        self._count(len(cs.new_problems))
        self._count(len(cs.current_abnormals))
        return {
            "last_visit_date": str(cs.last_visit_date) if cs.last_visit_date else None,
            "new_medications": [{"source_id": m.source_id, "name": m.text}
                                for m in cs.new_medications],
            "new_problems": [{"source_id": p.source_id, "name": p.text}
                             for p in cs.new_problems],
            "current_abnormals": [
                {"source_id": a.source_id, "name": a.name,
                 "value": round(a.numeric_value, 2) if a.numeric_value is not None else a.value,
                 "unit": a.unit, "status": a.status, "reference": a.reference_display}
                for a in cs.current_abnormals],
        }


# Anthropic tool schemas (names map to ContextTools methods).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "find_labs",
     "description": "Recent lab results matching a name or LOINC, with each result's "
                    "deterministic normal/abnormal verdict and reference range. Use for "
                    "any question about a lab value or trend (e.g. 'creatinine', 'potassium').",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["query"]}},
    {"name": "list_medications",
     "description": "All of the patient's medications with status and start date.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_problems",
     "description": "The patient's problem list.",
     "input_schema": {"type": "object",
                      "properties": {"active_only": {"type": "boolean"}}}},
    {"name": "list_allergies",
     "description": "The patient's documented allergies and criticality.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "latest_vitals",
     "description": "The most recent vital signs.",
     "input_schema": {"type": "object",
                      "properties": {"limit": {"type": "integer"}}}},
    {"name": "whats_changed",
     "description": "The deterministic change-set since the prior visit: new meds, "
                    "new problems, and currently abnormal or worsening labs.",
     "input_schema": {"type": "object", "properties": {}}},
]
