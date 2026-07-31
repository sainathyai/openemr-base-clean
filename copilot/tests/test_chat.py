"""UC-3 grounding tests: the conversational agent may only cite what a tool
returned and may only state numbers a tool surfaced. Plus the DQ-6 sanitation guard.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.chat import check_grounding
from app.fhir_client import _clean
from app.schemas import (
    Allergy, Demographics, LabResult, Medication, PatientContext,
)
from app.tools import ContextTools

FEMALE = Demographics(source_id="Patient/f", name="Jane Doe", sex="female",
                      birth_date=date(1951, 1, 1))
CREAT_ID = "Observation/creat"
MED_ID = "MedicationRequest/med"


def _ctx() -> PatientContext:
    return PatientContext(
        patient_id="p1", demographics=FEMALE,
        labs=[LabResult(source_id=CREAT_ID, loinc="2160-0", name="Creatinine",
                        value="1.95", unit="mg/dL",
                        effective=datetime(2025, 12, 1, tzinfo=timezone.utc))],
        medications=[Medication(source_id=MED_ID, text="Clopidogrel 75 MG Oral Tablet",
                                status="active", authored_on=date(2025, 6, 1))],
        allergies=[Allergy(source_id="AllergyIntolerance/a", text="Tree nut",
                           criticality="high")],
    )


# ---- DQ-6 sanitation ----

def test_clean_strips_template_placeholders():
    assert _clean("{entry.value}") is None
    assert _clean("") is None
    assert _clean("none") is None
    assert _clean("1.95") == "1.95"


# ---- grounding ledger ----

def test_find_labs_records_ids_and_numbers():
    tools = ContextTools(_ctx())
    rows = tools.find_labs("creatinine")
    assert rows and rows[0]["source_id"] == CREAT_ID
    assert rows[0]["status"] == "high"
    assert CREAT_ID in tools.returned_ids
    assert "1.95" in tools.returned_numbers          # the value
    assert "0.59" in tools.returned_numbers          # the reference bounds
    assert "1" in tools.returned_numbers             # cardinality of the result set


def test_faithful_answer_is_grounded():
    tools = ContextTools(_ctx())
    tools.find_labs("creatinine")
    v = check_grounding("Creatinine is 1.95 mg/dL, high.", [CREAT_ID], tools)
    assert v == []


def test_citation_not_returned_is_flagged():
    tools = ContextTools(_ctx())
    tools.find_labs("creatinine")
    v = check_grounding("Potassium is high.", ["Observation/ghost"], tools)
    assert any(x.rule == "ungrounded_citation" for x in v)


def test_number_not_surfaced_is_flagged():
    tools = ContextTools(_ctx())
    tools.find_labs("creatinine")
    v = check_grounding("Creatinine jumped to 7.7 mg/dL.", [CREAT_ID], tools)
    assert any(x.rule == "unsourced_number" for x in v)


def test_stating_a_count_is_grounded():
    tools = ContextTools(_ctx())
    tools.list_medications()
    v = check_grounding("There is 1 medication on file.", [MED_ID], tools)
    assert v == []


def test_reset_clears_the_ledger_between_turns():
    tools = ContextTools(_ctx())
    tools.find_labs("creatinine")
    tools.reset()
    assert not tools.returned_ids and not tools.returned_numbers
