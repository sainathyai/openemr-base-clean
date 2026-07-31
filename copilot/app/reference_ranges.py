"""Deterministic lab abnormality classification (resolves D-9).

Synthea/OpenEMR labs carry no reference ranges or abnormal flags (DQ-2). This
module applies a documented, LOINC-keyed, unit-checked, sex/age-aware fallback
table (data/reference_ranges.json) to decide normal vs abnormal WITHOUT the LLM.
The agent narrates these decisions; it never makes them. Every assessment carries
the reference interval and its provenance so an abnormality claim is auditable.
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from .schemas import Demographics, LabResult

_DATA = Path(__file__).parent / "data" / "reference_ranges.json"

Status = Literal[
    "normal", "low", "high", "critical_low", "critical_high",
    "no_range", "non_numeric", "unit_mismatch", "pediatric",
]

ABNORMAL: set[str] = {"low", "high", "critical_low", "critical_high"}
CRITICAL: set[str] = {"critical_low", "critical_high"}


class LabAssessment(BaseModel):
    """A lab result with a deterministic, sourced abnormality verdict."""
    source_id: str
    loinc: Optional[str]
    name: str
    value: Optional[str]
    numeric_value: Optional[float]
    unit: Optional[str]
    status: Status
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    reference_display: Optional[str] = None
    provenance: Optional[str] = None
    effective: Optional[object] = None  # datetime; kept loose to avoid re-import

    @property
    def is_abnormal(self) -> bool:
        return self.status in ABNORMAL

    @property
    def is_critical(self) -> bool:
        return self.status in CRITICAL


@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def table_version() -> str:
    return _table().get("_meta", {}).get("version", "unknown")


def _norm_unit(u: Optional[str]) -> str:
    return re.sub(r"\s+", "", (u or "").lower())


def _parse_value(raw: Optional[str]) -> Optional[float]:
    """~26% of Synthea results are non-numeric (DQ-4). Extract a leading number
    if present; otherwise return None so the caller marks it non_numeric."""
    if raw is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
    return float(m.group()) if m else None


def _age(birth: Optional[date], asof: Optional[date] = None) -> Optional[int]:
    if not birth:
        return None
    asof = asof or date.today()
    return asof.year - birth.year - ((asof.month, asof.day) < (birth.month, birth.day))


def _pick_range(ranges: list[dict], sex: Optional[str]) -> dict:
    if sex:
        for r in ranges:
            if r.get("sex") == sex:
                return r
    for r in ranges:
        if "sex" not in r:
            return r
    return ranges[0]


def _fmt(low, high, unit: str) -> str:
    if low is not None and high is not None:
        return f"{low}-{high} {unit}"
    if low is not None:
        return f">={low} {unit}"
    if high is not None:
        return f"<{high} {unit}"
    return unit


def _verdict(val: float, direction: str, r: dict) -> Status:
    clo, chi = r.get("critical_low"), r.get("critical_high")
    lo, hi = r.get("low"), r.get("high")
    if clo is not None and val <= clo:
        return "critical_low"
    if chi is not None and val >= chi:
        return "critical_high"
    if direction != "lower_better" and lo is not None and val < lo:
        return "low"
    if direction != "higher_better" and hi is not None and val > hi:
        return "high"
    return "normal"


def classify(lab: LabResult, demo: Optional[Demographics]) -> LabAssessment:
    """Classify one lab against the reference table. Pure and deterministic."""
    sex = (demo.sex or "").lower() if demo else None
    sex = sex if sex in {"male", "female"} else None
    age = _age(demo.birth_date if demo else None,
               lab.effective.date() if lab.effective else None)

    base = LabAssessment(
        source_id=lab.source_id, loinc=lab.loinc, name=lab.name,
        value=lab.value, numeric_value=None, unit=lab.unit,
        status="no_range", effective=lab.effective,
    )

    tbl = _table()
    analyte_key = tbl["loinc_index"].get(lab.loinc or "")
    if not analyte_key:
        return base
    analyte = tbl["analytes"][analyte_key]
    base.provenance = analyte.get("source")

    # Unit gate: only classify when the reported unit matches (unit_policy).
    if lab.unit:
        allowed = {_norm_unit(analyte["unit"])} | {
            _norm_unit(u) for u in analyte.get("unit_aliases", [])
        }
        if _norm_unit(lab.unit) not in allowed:
            base.status = "unit_mismatch"
            return base

    num = _parse_value(lab.value)
    if num is None:
        base.status = "non_numeric"
        return base
    base.numeric_value = num

    if age is not None and age < 18:
        base.status = "pediatric"  # adult ranges invalid (pediatric_policy)
        return base

    r = _pick_range(analyte["ranges"], sex)
    base.reference_low = r.get("low")
    base.reference_high = r.get("high")
    base.reference_display = _fmt(r.get("low"), r.get("high"), analyte["unit"])
    base.status = _verdict(num, analyte.get("direction", "bidirectional"), r)
    return base


def classify_all(labs: list[LabResult], demo: Optional[Demographics]) -> list[LabAssessment]:
    return [classify(l, demo) for l in labs]
