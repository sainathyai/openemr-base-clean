"""UC-2 order safety check. Deterministic, sourced, LLM-free (D-8 principle).

A physician proposes a medication order as free text. This engine checks it against
the patient's real chart using a small curated knowledge table (data/order_knowledge
.json), mirroring the D-9 reference-range approach: the clinical judgement lives in an
auditable table, not in a model. It emits sourced findings across four axes:

  1. allergy        - documented allergy or class cross-reactivity (fails to DANGER)
  2. renal          - renally-handled drug against the patient's creatinine/eGFR
  3. duplicate/bleed - same-class therapy already on file, or additive bleeding risk
  4. indication/CI  - the active problem list supports or contraindicates the order

Every finding cites the FHIR source_id(s) it rests on. A drug that is not in the
table is reported as 'not recognized' with NO automated clearance: silence is never
treated as safety.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .reference_ranges import classify_all
from .schemas import PatientContext

_DATA = Path(__file__).parent / "data" / "order_knowledge.json"
_MIN = datetime.min.replace(tzinfo=timezone.utc)

Severity = Literal["danger", "caution", "info", "ok"]
_RANK = {"ok": 0, "info": 1, "caution": 2, "danger": 3}


class OrderFinding(BaseModel):
    severity: Severity
    category: str            # allergy | renal | duplicate | bleeding | indication | contraindication | unrecognized | clear
    title: str
    detail: str
    source_ids: list[str] = Field(default_factory=list)


class OrderCheckResult(BaseModel):
    order_text: str
    recognized: bool
    matched_drug: Optional[str] = None
    drug_class: Optional[str] = None
    overall: Severity = "ok"
    findings: list[OrderFinding] = Field(default_factory=list)
    considered: dict = Field(default_factory=dict)
    grounded: bool = True     # findings are rendered from source data, not a model


@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def table_version() -> str:
    return _table().get("_meta", {}).get("version", "unknown")


def _aware(dt) -> datetime:
    if dt is None:
        return _MIN
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _match_drug(text: str) -> Optional[str]:
    """Longest table key that appears in the order text (generic names)."""
    t = (text or "").lower()
    hits = [k for k in _table()["drugs"] if k in t]
    return max(hits, key=len) if hits else None


# Problems phrased as historical/negated/contextual are not an active clinical
# state, so they must not drive an indication or contraindication. Synthea leans on
# "history of ... (situation)" heavily (e.g. "Past pregnancy history of miscarriage"),
# which naive substring matching would misread as a live contraindication.
_HISTORY_MARKERS = ("history", "h/o", "status post", "past ", "prior ",
                    "family history", "no evidence", "(situation)")


def _is_active_state(text: str) -> bool:
    t = (text or "").lower()
    return not any(m in t for m in _HISTORY_MARKERS)


def _kw_hit(keyword: str, text: str) -> bool:
    """Whole-word (phrase) match, so 'pregnancy' does not fire inside 'past
    pregnancy history' via _is_active_state, and short keywords do not partial-match."""
    return re.search(r"\b" + re.escape(keyword) + r"\b", text) is not None


def _active_problems(ctx: PatientContext) -> list:
    return [p for p in ctx.problems
            if (p.clinical_status or "active").lower() in {"active", ""}]


def _med_key(med_text: str) -> Optional[str]:
    return _match_drug(med_text)


def _val(a) -> str:
    """Render a lab value without the raw-float tail (backfill values carry many
    decimals); mirrors how the briefing rounds to 2 places."""
    if a.numeric_value is not None:
        return f"{round(a.numeric_value, 2)}"
    return a.value


def _dedupe(findings: list[OrderFinding]) -> list[OrderFinding]:
    """Collapse identical findings (e.g. a med with two refill entries produces the
    same bleeding/duplicate flag twice), merging their citations onto one card."""
    seen: dict[tuple, OrderFinding] = {}
    order: list[tuple] = []
    for f in findings:
        k = (f.severity, f.category, f.title, f.detail)
        if k in seen:
            for sid in f.source_ids:
                if sid not in seen[k].source_ids:
                    seen[k].source_ids.append(sid)
        else:
            seen[k] = f
            order.append(k)
    return [seen[k] for k in order]


def _latest(assessments, *needles: str):
    """Most recent assessment whose name contains any needle."""
    hits = [a for a in assessments
            if any(n in (a.name or "").lower() for n in needles)]
    hits.sort(key=lambda a: _aware(a.effective), reverse=True)
    return hits[0] if hits else None


def check_order(order_text: str, ctx: PatientContext) -> OrderCheckResult:
    tbl = _table()
    key = _match_drug(order_text)
    considered = {
        "allergies_checked": len(ctx.allergies),
        "active_meds_compared": 0,
        "active_problems_compared": 0,
        "renal_labs_found": False,
        "table_version": table_version(),
    }

    if key is None:
        return OrderCheckResult(
            order_text=order_text, recognized=False, considered=considered,
            overall="caution",
            findings=[OrderFinding(
                severity="caution", category="unrecognized",
                title="Order not recognized",
                detail="This drug is not in the demo knowledge base, so no automated "
                       "allergy, renal, duplicate, or indication checks were run. "
                       "Review manually.")],
        )

    drug = tbl["drugs"][key]
    findings: list[OrderFinding] = []

    # ---- 1. allergy ----
    a_class = drug.get("allergen_class")
    cross = tbl.get("allergen_cross_reactivity", {})
    for al in ctx.allergies:
        atext = (al.text or "").lower()
        if not atext:
            continue
        if key in atext or drug["display"].lower() in atext:
            findings.append(OrderFinding(
                severity="danger", category="allergy",
                title="Documented allergy to this drug",
                detail=f"Chart lists an allergy to {al.text}.",
                source_ids=[al.source_id]))
        elif a_class and a_class in atext:
            findings.append(OrderFinding(
                severity="danger", category="allergy",
                title=f"Allergy to the {a_class} class",
                detail=f"Chart lists a {al.text} allergy; {drug['display']} is a "
                       f"{a_class}-class agent.",
                source_ids=[al.source_id]))
        else:
            # partial cross-reactivity (e.g. penicillin allergy -> cephalosporin)
            for pat_allergen, cross_classes in cross.items():
                if pat_allergen in atext and a_class in cross_classes:
                    findings.append(OrderFinding(
                        severity="caution", category="allergy",
                        title=f"Possible cross-reactivity with {pat_allergen} allergy",
                        detail=f"Chart lists a {al.text} allergy; {drug['display']} "
                               f"({a_class}) can partially cross-react.",
                        source_ids=[al.source_id]))

    # ---- 2. renal ----
    renal = drug.get("renal", {})
    if renal.get("risk"):
        assessments = classify_all(ctx.labs, ctx.demographics)
        creat = _latest(assessments, "creatinine")
        egfr = _latest(assessments, "glomerular filtration", "egfr")
        considered["renal_labs_found"] = bool(creat or egfr)
        renal_abn = None
        for a in (egfr, creat):
            if a and a.status in {"low", "high", "critical_low", "critical_high"}:
                renal_abn = a
                break
        ckd_contra = "chronic kidney disease" in drug.get("contraindications", [])
        if renal_abn is not None:
            sev = "danger" if ckd_contra else "caution"
            findings.append(OrderFinding(
                severity=sev, category="renal",
                title="Renal function is abnormal",
                detail=f"{renal_abn.name} = {_val(renal_abn)} {renal_abn.unit or ''} "
                       f"({renal_abn.status}, ref {renal_abn.reference_display}). "
                       f"{renal['note']}.",
                source_ids=[renal_abn.source_id]))
        elif creat or egfr:
            ref = creat or egfr
            findings.append(OrderFinding(
                severity="info", category="renal",
                title="Renally handled; current renal function normal",
                detail=f"{ref.name} = {_val(ref)} {ref.unit or ''} is within range "
                       f"({ref.reference_display}). {renal['note']}.",
                source_ids=[ref.source_id]))
        else:
            findings.append(OrderFinding(
                severity="caution", category="renal",
                title="Renal function not on file",
                detail=f"{drug['display']} is renally handled and no recent "
                       f"creatinine/eGFR is available. {renal['note']}."))

    # ---- 3. duplicate therapy + additive bleeding risk ----
    dclass = drug["drug_class"]
    dgroups = set(drug.get("groups", []))
    bleed_groups = set(tbl.get("bleeding_risk_groups", []))
    active_meds = [m for m in ctx.medications
                   if (m.status or "active").lower() in {"active", ""}]
    considered["active_meds_compared"] = len(active_meds)
    for m in active_meds:
        mk = _med_key(m.text)
        if not mk:
            continue
        md = tbl["drugs"][mk]
        if md["drug_class"] == dclass:
            same = mk == key
            findings.append(OrderFinding(
                severity="caution", category="duplicate",
                title="Already on the same drug" if same else "Duplicate therapy (same class)",
                detail=f"Patient is already on {m.text} "
                       f"({md['drug_class']}).",
                source_ids=[m.source_id]))
        elif dgroups & bleed_groups and set(md.get("groups", [])) & bleed_groups:
            findings.append(OrderFinding(
                severity="caution", category="bleeding",
                title="Additive bleeding risk",
                detail=f"{drug['display']} plus existing {m.text} "
                       f"({md['drug_class']}) raises bleeding risk.",
                source_ids=[m.source_id]))

    # ---- 4. indication / contraindication from the problem list ----
    problems = _active_problems(ctx)
    considered["active_problems_compared"] = len(problems)
    inds = [s.lower() for s in drug.get("indications", [])]
    cis = [s.lower() for s in drug.get("contraindications", [])]
    ind_hit = False
    for p in problems:
        if not _is_active_state(p.text):
            continue   # historical/contextual entry, not a live indication or CI
        ptext = (p.text or "").lower()
        if any(_kw_hit(ci, ptext) for ci in cis):
            findings.append(OrderFinding(
                severity="danger", category="contraindication",
                title="Contraindicated by an active problem",
                detail=f"{drug['display']} is contraindicated with {p.text}.",
                source_ids=[p.source_id]))
        elif not ind_hit and any(_kw_hit(ind, ptext) for ind in inds):
            ind_hit = True
            findings.append(OrderFinding(
                severity="info", category="indication",
                title="Supported by an active problem",
                detail=f"{drug['display']} is an appropriate choice for {p.text}.",
                source_ids=[p.source_id]))

    if not findings:
        findings.append(OrderFinding(
            severity="ok", category="clear",
            title="No safety flags",
            detail=f"No allergy, renal, duplicate, or contraindication flags for "
                   f"{drug['display']} against the current chart."))

    findings = _dedupe(findings)
    findings.sort(key=lambda f: _RANK[f.severity], reverse=True)
    overall = findings[0].severity
    return OrderCheckResult(
        order_text=order_text, recognized=True,
        matched_drug=drug["display"], drug_class=dclass,
        overall=overall, findings=findings, considered=considered,
    )
