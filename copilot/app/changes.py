"""Deterministic 'what changed since last visit' computation (UC-1).

This is the pre-visit synthesis engine. It runs with NO LLM: it classifies labs
(reference_ranges), finds the prior visit, and computes the concrete deltas a
physician cares about (new problems, new meds, critical values, worsening lab
trends). The output ChangeSet is the ONLY clinical material the LLM is later
allowed to narrate, so every downstream sentence traces to a fact computed here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .reference_ranges import ABNORMAL, CRITICAL, LabAssessment, classify_all
from .schemas import Medication, PatientContext, Problem

_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _aware(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return _MIN
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


TrendFlag = str  # newly_abnormal | worsening | improving | resolved | stable_abnormal | stable_normal


class LabTrend(BaseModel):
    loinc: Optional[str]
    name: str
    unit: Optional[str]
    latest_value: Optional[float]
    latest_status: str
    latest_date: Optional[datetime]
    prior_value: Optional[float] = None
    prior_status: Optional[str] = None
    prior_date: Optional[datetime] = None
    delta: Optional[float] = None
    reference_display: Optional[str] = None
    flag: TrendFlag
    latest_source_id: str
    prior_source_id: Optional[str] = None

    @property
    def is_notable(self) -> bool:
        return self.flag in {"newly_abnormal", "worsening"}


class ChangeSet(BaseModel):
    """The deterministic delta the LLM narrates. Nothing here is LLM-authored."""
    patient_id: str
    patient_name: Optional[str] = None
    last_visit_date: Optional[date] = None
    prior_visit_date: Optional[date] = None
    new_problems: list[Problem] = Field(default_factory=list)
    new_medications: list[Medication] = Field(default_factory=list)
    current_criticals: list[LabAssessment] = Field(default_factory=list)
    current_abnormals: list[LabAssessment] = Field(default_factory=list)
    trends: list[LabTrend] = Field(default_factory=list)
    high_risk_allergies: list[str] = Field(default_factory=list)
    active_problem_count: int = 0
    medication_count: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def notable_trends(self) -> list[LabTrend]:
        return [t for t in self.trends if t.is_notable]

    @property
    def has_changes(self) -> bool:
        return bool(self.new_problems or self.new_medications
                    or self.current_abnormals or self.notable_trends)


def _transition(prev: Optional[str], now: str) -> TrendFlag:
    prev_abn = prev in ABNORMAL if prev else False
    now_abn = now in ABNORMAL
    if prev is None or prev not in ABNORMAL | {"normal"}:
        return "stable_abnormal" if now_abn else "stable_normal"
    if not prev_abn and now_abn:
        return "newly_abnormal"
    if prev_abn and not now_abn:
        return "resolved"
    if prev_abn and now_abn:
        # both abnormal: worsening if it moved into critical or further out
        if now in CRITICAL and prev not in CRITICAL:
            return "worsening"
        return "stable_abnormal"
    return "stable_normal"


_WORSEN_REL = 0.02  # ignore <2% drift to avoid noise on synthetic fluctuation


def _worsening_by_value(prev_v: float, now_v: float, prev_s: str, now_s: str) -> bool:
    """Both abnormal on the same side and moved meaningfully further from normal."""
    if prev_v is None or now_v is None:
        return False
    rel = abs(now_v - prev_v) / max(abs(prev_v), 1e-9)
    if rel < _WORSEN_REL:
        return False
    if prev_s in {"high", "critical_high"} and now_s in {"high", "critical_high"}:
        return now_v > prev_v
    if prev_s in {"low", "critical_low"} and now_s in {"low", "critical_low"}:
        return now_v < prev_v
    return False


def compute_changes(ctx: PatientContext) -> ChangeSet:
    demo = ctx.demographics
    assessed = classify_all(ctx.labs, demo)

    # --- find the prior visit boundary ---
    enc_dates = sorted({e.date.date() for e in ctx.encounters if e.date}, reverse=True)
    last_visit = enc_dates[0] if enc_dates else None
    prior_visit = enc_dates[1] if len(enc_dates) > 1 else None
    # "New since last visit" means dated strictly after the prior visit.
    cutoff = prior_visit or last_visit

    cs = ChangeSet(
        patient_id=ctx.patient_id,
        patient_name=demo.name if demo else None,
        last_visit_date=last_visit,
        prior_visit_date=prior_visit,
        active_problem_count=sum(
            1 for p in ctx.problems
            if (p.clinical_status or "").lower() in {"active", ""} or p.clinical_status is None
        ),
        medication_count=len(ctx.medications),
    )

    if cutoff:
        cs.new_problems = [p for p in ctx.problems if p.onset and p.onset > cutoff]
        cs.new_medications = [m for m in ctx.medications if m.authored_on and m.authored_on > cutoff]

    cs.high_risk_allergies = [
        a.text for a in ctx.allergies
        if (a.criticality or "").lower() in {"high", "critical"}
    ]

    # --- lab trends, grouped by LOINC (specimen-specific) ---
    by_code: dict[str, list[LabAssessment]] = {}
    for a in assessed:
        if a.numeric_value is None or not a.loinc:
            continue
        by_code.setdefault(a.loinc, []).append(a)

    for loinc, series in by_code.items():
        series.sort(key=lambda x: _aware(x.effective), reverse=True)
        latest = series[0]
        prior = series[1] if len(series) > 1 else None
        flag = _transition(prior.status if prior else None, latest.status)
        # already abnormal and drifting further out counts as worsening
        if (prior and latest.is_abnormal and prior.status in ABNORMAL
                and _worsening_by_value(prior.numeric_value, latest.numeric_value,
                                        prior.status, latest.status)):
            flag = "worsening"
        cs.trends.append(LabTrend(
            loinc=loinc, name=latest.name, unit=latest.unit,
            latest_value=latest.numeric_value, latest_status=latest.status,
            latest_date=latest.effective,
            prior_value=prior.numeric_value if prior else None,
            prior_status=prior.status if prior else None,
            prior_date=prior.effective if prior else None,
            delta=(latest.numeric_value - prior.numeric_value) if prior else None,
            reference_display=latest.reference_display,
            flag=flag,
            latest_source_id=latest.source_id,
            prior_source_id=prior.source_id if prior else None,
        ))
        if latest.is_abnormal:
            cs.current_abnormals.append(latest)
        if latest.is_critical:
            cs.current_criticals.append(latest)

    # order abnormals worst-first: criticals ahead of the rest
    cs.current_abnormals.sort(key=lambda a: (not a.is_critical, a.name))
    # stable sort of trends: notable first, then by name
    cs.trends.sort(key=lambda t: (not t.is_notable, t.name))
    return cs
