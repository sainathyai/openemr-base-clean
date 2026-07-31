"""Boundary/invariant tests for the deterministic lab classifier (D-9).

These are the abnormality decisions the whole verification story rests on, so they
are pinned here and run in CI with no live OpenEMR.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.reference_ranges import classify
from app.schemas import Demographics, LabResult

MALE = Demographics(source_id="Patient/m", name="M", sex="male", birth_date=date(1980, 1, 1))
FEMALE = Demographics(source_id="Patient/f", name="F", sex="female", birth_date=date(1980, 1, 1))
CHILD = Demographics(source_id="Patient/c", name="C", sex="male", birth_date=date(2015, 1, 1))
ASOF = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _lab(loinc, value, unit, name="lab"):
    return LabResult(source_id=f"Observation/{loinc}-{value}", loinc=loinc,
                     name=name, value=str(value), unit=unit, effective=ASOF)


@pytest.mark.parametrize("value,expected", [
    (4.2, "normal"),
    (5.8, "high"),
    (3.0, "low"),
    (6.9, "critical_high"),
    (2.1, "critical_low"),
])
def test_potassium_bands(value, expected):
    # potassium 2823-3, ref 3.5-5.1, crit <=2.5 / >=6.5
    assert classify(_lab("2823-3", value, "mmol/L"), MALE).status == expected


def test_creatinine_is_sex_specific():
    # 1.2 mg/dL: normal for a man (<=1.35), high for a woman (>1.04)
    assert classify(_lab("2160-0", 1.2, "mg/dL"), MALE).status == "normal"
    assert classify(_lab("2160-0", 1.2, "mg/dL"), FEMALE).status == "high"


def test_unit_mismatch_is_not_classified():
    a = classify(_lab("2823-3", 6.0, "mg/dL"), MALE)  # potassium wrong unit
    assert a.status == "unit_mismatch"
    assert a.reference_low is None


def test_non_numeric_value():
    a = classify(_lab("2823-3", "positive", "mmol/L"), MALE)
    assert a.status == "non_numeric"


def test_pediatric_gets_no_adult_range():
    a = classify(_lab("2823-3", 6.0, "mmol/L"), CHILD)
    assert a.status == "pediatric"


def test_unknown_loinc_has_no_range():
    a = classify(_lab("99999-9", 1.0, "mmol/L"), MALE)
    assert a.status == "no_range"


def test_hdl_is_inverted_higher_is_better():
    # HDL 2085-9, low <40 is the risk; a high value is never flagged high
    assert classify(_lab("2085-9", 30, "mg/dL"), MALE).status == "low"
    assert classify(_lab("2085-9", 80, "mg/dL"), MALE).status == "normal"


def test_cholesterol_is_target_based_no_low_flag():
    # total cholesterol 2093-3, desirable <200, high >=240; never "low"
    assert classify(_lab("2093-3", 260, "mg/dL"), MALE).status == "critical_high"
    assert classify(_lab("2093-3", 150, "mg/dL"), MALE).status == "normal"


def test_provenance_is_always_attached_for_known_analyte():
    a = classify(_lab("2823-3", 4.2, "mmol/L"), MALE)
    assert a.provenance and a.reference_display == "3.5-5.1 mmol/L"
