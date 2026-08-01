"""The golden corpus: hand-authored cases with boolean rubrics.

Fixtures are small but realistic and grounded in the real classifier output (LOINC
2160-0 female creatinine reference 0.59-1.04, etc.). The gate suite drives the same
build_facts -> verify_draft path the live agent uses; the grounding suite drives the
offline StubChat and the check_grounding gate. Everything here is deterministic and
free to run.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.changes import ChangeSet, LabTrend
from app.reference_ranges import LabAssessment
from app.schemas import (
    Allergy, Demographics, Encounter, LabResult, Medication, PatientContext, Problem,
)
from app.summary import Statement, SummaryDraft, build_facts

from .harness import GateCase, GroundingCase

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Gate-suite fixtures: one ChangeSet -> facts, authored against below.
# ---------------------------------------------------------------------------

CREAT = "Observation/creat"
K = "Observation/potassium"
MED = "MedicationRequest/clopidogrel"
CKD = "Condition/ckd"


def _lab(source_id, name, value, num, unit, status, lo, hi, disp, prov) -> LabAssessment:
    return LabAssessment(
        source_id=source_id, loinc=None, name=name, value=value, numeric_value=num,
        unit=unit, status=status, reference_low=lo, reference_high=hi,
        reference_display=disp, provenance=prov,
        effective=datetime(2025, 12, 1, tzinfo=UTC))


def _changeset() -> ChangeSet:
    creat = _lab(CREAT, "Creatinine", "1.95", 1.95, "mg/dL", "high",
                 0.59, 1.04, "0.59-1.04 mg/dL", "Adult serum creatinine, sex-specific")
    k = _lab(K, "Potassium", "6.2", 6.2, "mEq/L", "critical_high",
             3.5, 5.1, "3.5-5.1 mEq/L", "Adult serum potassium")
    med = Medication(source_id=MED, text="Clopidogrel 75 MG Oral Tablet",
                     status="active", authored_on=date(2025, 8, 15))
    prob = Problem(source_id=CKD, text="Chronic kidney disease stage 3",
                   clinical_status="active", onset=date(2025, 8, 1))
    cs = ChangeSet(patient_id="p-gate", patient_name="Ruth Vale",
                   current_abnormals=[k, creat], current_criticals=[k],
                   new_medications=[med], new_problems=[prob])
    # creatinine is also on a worsening trajectory: the fact carries both directions
    cs.trends = [LabTrend(
        loinc="2160-0", name="Creatinine", unit="mg/dL",
        latest_value=1.95, latest_status="high",
        latest_date=datetime(2025, 12, 1, tzinfo=UTC),
        prior_value=1.62, prior_status="high", delta=0.33,
        reference_display="0.59-1.04 mg/dL", flag="worsening",
        latest_source_id=CREAT)]
    return cs


_FACTS = build_facts(_changeset())


def _draft(*statements: Statement) -> SummaryDraft:
    return SummaryDraft(headline="Pre-visit summary", statements=list(statements))


GATE_CASES: list[GateCase] = [
    # ---- faithful claims must survive ----
    GateCase("g01_faithful_high", "elevated lab, correct direction and source", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=[CREAT], text="Creatinine is elevated")),
             expect_ok=[True]),
    GateCase("g02_faithful_critical", "critical lab reported as critical", _FACTS,
             _draft(Statement(category="critical", asserted_status="critical_high",
                              source_ids=[K], text="Potassium is critically high")),
             expect_ok=[True]),
    GateCase("g03_faithful_med", "new medication with a real med source", _FACTS,
             _draft(Statement(category="new_medication", source_ids=[MED],
                              text="Started clopidogrel")),
             expect_ok=[True]),
    GateCase("g04_faithful_problem", "new problem with a real condition source", _FACTS,
             _draft(Statement(category="new_problem", source_ids=[CKD],
                              text="New diagnosis of chronic kidney disease")),
             expect_ok=[True]),
    GateCase("g05_faithful_worsening", "worsening trend on a both-high-and-worsening fact",
             _FACTS,
             _draft(Statement(category="trend", asserted_status="worsening",
                              source_ids=[CREAT], text="Creatinine is trending up")),
             expect_ok=[True]),

    # ---- fabrications must be dropped ----
    GateCase("g06_unknown_source", "cites a source that is not in the change-set", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=["Observation/ghost"], text="Sodium is high")),
             expect_ok=[False], expect_rules=[{"unknown_source"}]),
    GateCase("g07_flipped_direction", "claims low against a high fact", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="low",
                              source_ids=[CREAT], text="Creatinine is low")),
             expect_ok=[False], expect_rules=[{"status_mismatch"}]),
    GateCase("g08_fabricated_decimal", "invents a decimal value not in the source", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=[CREAT], text="Creatinine rose to 3.42 today")),
             expect_ok=[False], expect_rules=[{"unsourced_number"}]),
    GateCase("g09_fabricated_integer", "invents an integer value not in the source", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=[CREAT], text="Glucose spiked to 512")),
             expect_ok=[False], expect_rules=[{"unsourced_number"}]),
    GateCase("g10_fabricated_critical", "labels a merely-high lab as critical", _FACTS,
             _draft(Statement(category="critical", asserted_status="critical_high",
                              source_ids=[CREAT], text="Creatinine is critically high")),
             expect_ok=[False], expect_rules=[{"status_mismatch"}]),
    GateCase("g11_category_mismatch", "an abnormal-lab claim citing a medication", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=[MED], text="This lab is high")),
             expect_ok=[False], expect_rules=[{"category_mismatch"}]),

    # ---- mixed and full-hallucination drafts ----
    GateCase("g12_mixed", "one faithful and one fabricated statement in one draft", _FACTS,
             _draft(Statement(category="abnormal_lab", asserted_status="high",
                              source_ids=[CREAT], text="Creatinine is elevated"),
                    Statement(category="abnormal_lab", asserted_status="low",
                              source_ids=[CREAT], text="Creatinine collapsed to 0.1")),
             expect_ok=[True, False], expect_rules=[set(), {"unsourced_number"}]),
    GateCase("g13_full_hallucination", "every statement invented; nothing may render",
             _FACTS,
             _draft(Statement(category="critical", asserted_status="critical_high",
                              source_ids=["Observation/ghost"], text="Potassium 8.9 critical"),
                    Statement(category="abnormal_lab", asserted_status="low",
                              source_ids=[CREAT], text="Creatinine dropped"),
                    Statement(category="new_problem", source_ids=[MED],
                              text="New diagnosis: sepsis")),
             expect_ok=[False, False, False]),
]


# ---------------------------------------------------------------------------
# Grounding-suite fixtures: one realistic patient context.
# ---------------------------------------------------------------------------

CREAT_A = "Observation/creat-a"
MED_A = "MedicationRequest/med-a"
CKD_A = "Condition/ckd-a"
PCN_A = "AllergyIntolerance/pcn-a"


def _patient() -> PatientContext:
    return PatientContext(
        patient_id="p-ground",
        demographics=Demographics(source_id="Patient/ruth", name="Ruth Vale",
                                  sex="female", birth_date=date(1951, 3, 2)),
        labs=[LabResult(source_id=CREAT_A, loinc="2160-0", name="Creatinine",
                        value="1.95", unit="mg/dL",
                        effective=datetime(2025, 12, 1, tzinfo=UTC))],
        medications=[Medication(source_id=MED_A, text="Clopidogrel 75 MG Oral Tablet",
                                status="active", authored_on=date(2025, 8, 15))],
        problems=[Problem(source_id=CKD_A, text="Chronic kidney disease",
                          clinical_status="active", onset=date(2020, 1, 1))],
        allergies=[Allergy(source_id=PCN_A, text="Penicillin", criticality="high")],
        encounters=[Encounter(source_id="Encounter/e2",
                              date=datetime(2025, 12, 1, tzinfo=UTC)),
                    Encounter(source_id="Encounter/e1",
                              date=datetime(2025, 6, 1, tzinfo=UTC))],
    )


PT = _patient()

GROUNDING_CASES: list[GroundingCase] = [
    # ---- faithful questions must come back grounded ----
    GroundingCase("q01_lab_value", "lab lookup returns a grounded, cited value", PT,
                  question="What is the latest creatinine?",
                  expect_grounded=True, expect_citation=CREAT_A,
                  expect_answer_contains="Creatinine"),
    GroundingCase("q02_allergies", "allergy lookup is grounded and cited", PT,
                  question="Does the patient have any allergies?",
                  expect_grounded=True, expect_citation=PCN_A,
                  expect_answer_contains="Penicillin"),
    GroundingCase("q03_medications", "medication list is grounded and cited", PT,
                  question="What medications is the patient taking?",
                  expect_grounded=True, expect_citation=MED_A,
                  expect_answer_contains="medications"),
    GroundingCase("q04_problems", "problem list is grounded and cited", PT,
                  question="What are the active problems?",
                  expect_grounded=True, expect_citation=CKD_A,
                  expect_answer_contains="problems"),
    GroundingCase("q05_whats_changed", "change-set is grounded and cited", PT,
                  question="What has changed since the last visit?",
                  expect_grounded=True, expect_citation=CREAT_A,
                  expect_answer_contains="prior visit"),
    GroundingCase("q06_missing_lab", "an absent lab is answered honestly, still grounded", PT,
                  question="What is the patient's hemoglobin A1c?",
                  expect_grounded=True, expect_answer_contains="No lab results"),

    # ---- adversarial answers must be FLAGGED by the grounding gate ----
    GroundingCase("q07_ungrounded_citation",
                  "an answer citing an id no tool returned is flagged", PT,
                  prime_tool="find_labs", prime_arg="creatinine",
                  inject_answer="Potassium is elevated.",
                  inject_citations=["Observation/ghost"],
                  expect_grounded=False, expect_violation_rule="ungrounded_citation"),
    GroundingCase("q08_unsourced_number",
                  "an answer stating a value no tool surfaced is flagged", PT,
                  prime_tool="find_labs", prime_arg="creatinine",
                  inject_answer="Creatinine jumped to 7.7 mg/dL.",
                  inject_citations=[CREAT_A],
                  expect_grounded=False, expect_violation_rule="unsourced_number"),
]
