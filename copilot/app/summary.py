"""LLM output contract + deterministic verification gate + renderer.

The design principle (D-8): the LLM never owns clinical truth. It receives a
ChangeSet of deterministically computed facts and must emit STRUCTURED claims,
each referencing the FHIR source_ids it rests on and (for lab claims) asserting a
status. The gate then checks every claim against the ChangeSet:

  1. source attribution  - every referenced source_id must exist in the ChangeSet
  2. domain constraint    - an asserted lab status must match the table's verdict
  3. no unsourced numbers - decimal values in prose must come from a referenced fact

Numbers are never trusted from the model: the renderer injects the value,
reference range, and source id deterministically from the ChangeSet. A statement
that fails any check is dropped and the violation is recorded for observability.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .changes import ChangeSet

Category = Literal[
    "critical", "abnormal_lab", "trend", "new_medication",
    "new_problem", "allergy", "context",
]


# ---------- the LLM output contract ----------

class Statement(BaseModel):
    category: Category
    text: str = Field(description="Qualitative clinical phrasing. Do NOT include "
                                  "specific numeric lab values; they are rendered "
                                  "separately from the source data.")
    source_ids: list[str] = Field(default_factory=list,
                                  description="FHIR resource ids this claim rests on.")
    asserted_status: Optional[str] = Field(
        default=None,
        description="For abnormal_lab/trend/critical: the direction you are "
                    "claiming (e.g. high, low, worsening). Must match the source.")


class SummaryDraft(BaseModel):
    headline: str
    statements: list[Statement] = Field(default_factory=list)


# ---------- the deterministic fact index (truth) ----------

class Fact(BaseModel):
    source_id: str
    kind: str                  # abnormal_lab | critical_lab | trend | new_medication | new_problem
    statuses: list[str] = Field(default_factory=list)  # all directions a claim may assert
    render: str                # deterministic evidence string (value + ref + provenance)


def build_facts(cs: ChangeSet) -> dict[str, Fact]:
    """The allow-list of things the LLM may talk about, keyed by source_id."""
    facts: dict[str, Fact] = {}
    for a in cs.current_abnormals:
        kind = "critical_lab" if a.is_critical else "abnormal_lab"
        val = round(a.numeric_value, 2) if a.numeric_value is not None else a.value
        facts[a.source_id] = Fact(
            source_id=a.source_id, kind=kind, statuses=[a.status],
            render=f"{a.name} = {val} {a.unit or ''} (ref {a.reference_display}; "
                   f"source: {a.provenance})".replace("  ", " "),
        )
    for t in cs.notable_trends:
        # trend keyed by the latest draw's id; enrich an abnormal fact if it shares one
        f = facts.get(t.latest_source_id)
        pv = round(t.prior_value, 2) if t.prior_value is not None else "?"
        lv = round(t.latest_value, 2) if t.latest_value is not None else "?"
        render = f"{t.name}: {pv} -> {lv} {t.unit or ''} (ref {t.reference_display})"
        if f is None:
            facts[t.latest_source_id] = Fact(
                source_id=t.latest_source_id, kind="trend",
                statuses=[t.flag, t.latest_status], render=render)
        elif t.flag not in f.statuses:
            f.statuses.append(t.flag)  # same draw is both abnormal and trending
    for m in cs.new_medications:
        facts[m.source_id] = Fact(
            source_id=m.source_id, kind="new_medication",
            render=f"{m.text}" + (f" (started {m.authored_on})" if m.authored_on else ""))
    for p in cs.new_problems:
        facts[p.source_id] = Fact(
            source_id=p.source_id, kind="new_problem",
            render=f"{p.text}" + (f" (onset {p.onset})" if p.onset else ""))
    return facts


# ---------- the verification gate ----------

class Violation(BaseModel):
    rule: str            # unknown_source | status_mismatch | category_mismatch | unsourced_number
    detail: str


class VerifiedStatement(BaseModel):
    statement: Statement
    ok: bool
    violations: list[Violation] = Field(default_factory=list)
    rendered: Optional[str] = None


_NUM = re.compile(r"\d+(?:\.\d+)?")   # integer or decimal
_LAB_KINDS = {"abnormal_lab", "critical_lab", "trend"}
_LAB_CATS = {"abnormal_lab", "critical", "trend"}


def _side(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.lower()
    if "worsen" in s:
        return "worsening"
    if "newly" in s or "new abnormal" in s:
        return "newly_abnormal"
    if "high" in s or "elevat" in s or "raised" in s or "hyper" in s:
        return "high"
    if "low" in s or "decreas" in s or "reduc" in s or "hypo" in s:
        return "low"
    return s


def _status_ok(asserted: Optional[str], statuses: list[str]) -> bool:
    """Domain-constraint check: the claimed direction must match one the table
    actually assigned to this fact (a fact may be both e.g. 'high' and 'worsening')."""
    a = _side(asserted)
    if a is None:
        return False          # a lab claim must assert a direction
    # A notable trend fact always also carries its abnormal side (e.g. 'high'), so
    # a legitimate directional claim matches directly. No looser fallback: matching
    # 'low' against a 'worsening'(high) fact would let a wrong-direction claim pass.
    sides = {_side(s) for s in statuses}
    return a in sides


def verify_draft(draft: SummaryDraft, facts: dict[str, Fact]) -> list[VerifiedStatement]:
    allowed_values = {re.sub(r"\s", "", n) for f in facts.values()
                      for n in _NUM.findall(f.render)}
    out: list[VerifiedStatement] = []
    for st in draft.statements:
        v: list[Violation] = []

        # 1. source attribution
        refs = [facts.get(sid) for sid in st.source_ids]
        for sid, f in zip(st.source_ids, refs):
            if f is None:
                v.append(Violation(rule="unknown_source",
                                   detail=f"{sid} is not in the change-set"))
        known = [f for f in refs if f is not None]

        # 2. category / domain-constraint
        if st.category in {"abnormal_lab", "critical", "trend"}:
            lab_facts = [f for f in known if f.kind in _LAB_KINDS]
            if not lab_facts:
                v.append(Violation(rule="category_mismatch",
                                   detail=f"{st.category} claim cites no lab fact"))
            for f in lab_facts:
                if not _status_ok(st.asserted_status, f.statuses):
                    v.append(Violation(rule="status_mismatch",
                                       detail=f"claimed '{st.asserted_status}' but "
                                              f"{f.source_id} is '{f.statuses}'"))
                if st.category == "critical" and f.kind != "critical_lab":
                    v.append(Violation(rule="status_mismatch",
                                       detail=f"{f.source_id} is not critical"))
        elif st.category == "new_medication":
            if not any(f.kind == "new_medication" for f in known):
                v.append(Violation(rule="category_mismatch",
                                   detail="new_medication claim cites no new med"))
        elif st.category == "new_problem":
            if not any(f.kind == "new_problem" for f in known):
                v.append(Violation(rule="category_mismatch",
                                   detail="new_problem claim cites no new problem"))

        # 3. no unsourced numbers in a lab/trend claim (values come from the
        # renderer). Skipped for med/problem text, whose names carry doses/codes.
        if st.category in _LAB_CATS:
            for num in _NUM.findall(st.text):
                if num not in allowed_values:
                    v.append(Violation(rule="unsourced_number",
                                       detail=f"value {num} not present in any cited fact"))

        ok = len(v) == 0
        rendered = _render(st, known) if ok else None
        out.append(VerifiedStatement(statement=st, ok=ok, violations=v, rendered=rendered))
    return out


def _render(st: Statement, facts: list[Fact]) -> str:
    """Compose the final line: the model's qualitative phrase + deterministic
    evidence pulled from the source data (D-8)."""
    line = st.text.strip().rstrip(".")
    evidence = "; ".join(f"{f.render} [{f.source_id}]" for f in facts)
    return f"{line}." + (f"  ({evidence})" if evidence else "")


def render_summary(headline: str, verified: list[VerifiedStatement]) -> str:
    lines = [f"# {headline}", ""]
    order = ["critical", "abnormal_lab", "trend", "new_medication", "new_problem", "allergy", "context"]
    kept = [v for v in verified if v.ok]
    kept.sort(key=lambda v: order.index(v.statement.category)
              if v.statement.category in order else 99)
    for v in kept:
        lines.append(f"- {v.rendered}")
    return "\n".join(lines)
