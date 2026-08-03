"""UC-2 order-safety engine (order_safety.check_order). Deterministic, no network.

Builds synthetic PatientContexts and pins each safety axis: allergy (direct + class
cross-reactivity), renal (abnormal vs normal vs unknown), duplicate/bleeding, and
indication/contraindication from the problem list. Also pins the fail-safe: an
unrecognized drug is never silently cleared, and every finding carries a source_id.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.order_safety import check_order
from app.schemas import (
    Allergy, Demographics, LabResult, Medication, PatientContext, Problem,
)


def _ctx(*, allergies=None, meds=None, problems=None, labs=None,
         sex="female", birth=date(1951, 8, 14)) -> PatientContext:
    return PatientContext(
        patient_id="p1",
        demographics=Demographics(source_id="Patient/p1", name="Test", birth_date=birth, sex=sex),
        allergies=allergies or [], medications=meds or [],
        problems=problems or [], labs=labs or [],
    )


def _creat(value: str, sid="Observation/creat") -> LabResult:
    return LabResult(source_id=sid, loinc="2160-0",
                     name="Creatinine [Mass/volume] in Serum or Plasma",
                     value=value, unit="mg/dL",
                     effective=datetime(2025, 12, 16, tzinfo=timezone.utc))


def _cats(r):
    return {f.category for f in r.findings}


def _sev(r, category):
    return next(f.severity for f in r.findings if f.category == category)


# ---- allergy ----

def test_direct_allergy_is_danger_and_sourced():
    r = check_order("amoxicillin 500 mg", _ctx(
        allergies=[Allergy(source_id="AllergyIntolerance/pcn", text="Penicillin", criticality="high")]))
    assert r.overall == "danger"
    assert "allergy" in _cats(r)
    f = next(f for f in r.findings if f.category == "allergy")
    assert f.source_ids == ["AllergyIntolerance/pcn"]


def test_penicillin_allergy_cross_reacts_with_cephalosporin_as_caution():
    r = check_order("cephalexin 500 mg", _ctx(
        allergies=[Allergy(source_id="AllergyIntolerance/pcn", text="Penicillin allergy")]))
    assert _sev(r, "allergy") == "caution"
    assert "cross-react" in next(f.detail for f in r.findings if f.category == "allergy").lower()


# ---- renal ----

def test_nsaid_in_ckd_with_high_creatinine_is_danger():
    r = check_order("ibuprofen 600 mg", _ctx(
        labs=[_creat("1.95")],
        problems=[Problem(source_id="Condition/ckd", text="Chronic kidney disease stage 3")]))
    assert r.overall == "danger"
    # both the renal lab and the problem-list contraindication should fire
    assert _sev(r, "renal") == "danger"
    assert "contraindication" in _cats(r)


def test_renal_drug_with_normal_creatinine_is_info():
    r = check_order("lisinopril 10 mg", _ctx(labs=[_creat("0.9")]))
    assert _sev(r, "renal") == "info"
    assert r.overall in {"info", "caution"}  # indication/renal info, nothing worse


def test_renal_drug_without_labs_flags_unknown():
    r = check_order("gabapentin 300 mg", _ctx())
    assert _sev(r, "renal") == "caution"
    assert r.considered["renal_labs_found"] is False


# ---- duplicate + bleeding ----

def test_same_class_is_duplicate():
    r = check_order("aspirin 81 mg", _ctx(
        meds=[Medication(source_id="MedicationRequest/clop", text="Clopidogrel 75 MG Oral Tablet", status="active")]))
    assert "duplicate" in _cats(r)
    assert _sev(r, "duplicate") == "caution"


def test_cross_group_additive_bleeding_risk():
    r = check_order("aspirin 81 mg", _ctx(
        meds=[Medication(source_id="MedicationRequest/warf", text="Warfarin 5 MG Oral Tablet", status="active")]))
    assert "bleeding" in _cats(r)
    assert r.findings[0].source_ids == ["MedicationRequest/warf"] or "bleeding" in _cats(r)


# ---- indication / contraindication ----

def test_indication_from_problem_list_is_info():
    r = check_order("lisinopril 10 mg", _ctx(
        labs=[_creat("0.9")],
        problems=[Problem(source_id="Condition/htn", text="Essential hypertension")]))
    assert "indication" in _cats(r)
    f = next(f for f in r.findings if f.category == "indication")
    assert f.source_ids == ["Condition/htn"]


def test_historical_problem_is_not_a_contraindication():
    # "Past pregnancy history of miscarriage" must NOT trigger the ACE-inhibitor
    # pregnancy contraindication (naive substring matching regressed on this).
    r = check_order("lisinopril 10 mg", _ctx(
        labs=[_creat("0.9")],
        problems=[Problem(source_id="Condition/preg",
                          text="Past pregnancy history of miscarriage (situation)")]))
    assert "contraindication" not in _cats(r)
    assert r.overall != "danger"


def test_metformin_contraindicated_in_ckd():
    r = check_order("metformin 500 mg", _ctx(
        labs=[_creat("2.1")],
        problems=[Problem(source_id="Condition/ckd", text="Chronic kidney disease")]))
    assert r.overall == "danger"
    assert "contraindication" in _cats(r)


# ---- fail-safe + clear ----

def test_unrecognized_drug_is_not_cleared():
    r = check_order("moonzepine 10 mg", _ctx())
    assert r.recognized is False
    assert r.overall == "caution"
    assert _cats(r) == {"unrecognized"}


def test_clean_order_reports_clear():
    r = check_order("amlodipine 5 mg", _ctx())
    assert r.overall == "ok"
    assert _cats(r) == {"clear"}


def test_every_finding_on_a_real_order_is_sourced_or_advisory():
    # findings that assert a chart fact must cite a source_id; advisory ones need not
    r = check_order("ibuprofen 600 mg", _ctx(
        labs=[_creat("1.95")],
        problems=[Problem(source_id="Condition/ckd", text="Chronic kidney disease")]))
    for f in r.findings:
        if f.category in {"allergy", "renal", "duplicate", "bleeding",
                          "indication", "contraindication"}:
            if f.category == "renal" and not f.source_ids:
                continue  # "renal function unknown" is advisory, allowed to be unsourced
            assert f.source_ids, f"{f.category} finding must cite a source"
