"""FHIR value extraction, focused on the blood-pressure component[] parser (DQ-5).

Blood pressure has no top-level valueQuantity: systolic (LOINC 8480-6) and diastolic
(8462-4) live in component[]. Without a parser the vital surfaces as None. These
tests pin the panel -> "systolic/diastolic" rendering and its fallbacks, and guard
that ordinary value extraction and the DQ-6 placeholder scrub still hold.
"""
from __future__ import annotations

from app.fhir_client import _fmt_qty, _norm_bp_unit, _obs_value


def _bp(sys_val, dia_val, unit="mm[Hg]") -> dict:
    def comp(code, display, val):
        return {"code": {"coding": [{"system": "http://loinc.org",
                                     "code": code, "display": display}]},
                "valueQuantity": {"value": val, "unit": unit}}
    comps = []
    if sys_val is not None:
        comps.append(comp("8480-6", "Systolic blood pressure", sys_val))
    if dia_val is not None:
        comps.append(comp("8462-4", "Diastolic blood pressure", dia_val))
    return {"resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9",
                                "display": "Blood pressure panel"}]},
            "component": comps}


# ---- the BP panel ----

def test_full_bp_panel_renders_systolic_over_diastolic():
    val, unit = _obs_value(_bp(150, 90))
    assert val == "150/90"
    assert unit == "mmHg"          # mm[Hg] normalized


def test_bp_values_drop_trailing_zero():
    val, _ = _obs_value(_bp(120.0, 80.0))
    assert val == "120/80"


def test_bp_numbers_are_both_present_for_grounding():
    # the grounding ledger keys on the numbers in the rendered string
    val, _ = _obs_value(_bp(138, 88))
    assert "138" in val and "88" in val


def test_systolic_only_falls_back_to_the_single_component():
    val, unit = _obs_value(_bp(150, None))
    assert val == "150"
    assert unit == "mm[Hg]"        # no panel, so no BP-specific normalization


def test_component_without_a_value_is_not_surfaced():
    obs = {"resourceType": "Observation", "component": [
        {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}}]}
    assert _obs_value(obs) == (None, None)


# ---- ordinary extraction and DQ-6 must still hold ----

def test_plain_value_quantity_unchanged():
    obs = {"valueQuantity": {"value": 1.95, "unit": "mg/dL"}}
    assert _obs_value(obs) == ("1.95", "mg/dL")


def test_dq6_placeholder_still_scrubbed():
    assert _obs_value({"valueString": "{entry.value}"}) == (None, None)


def test_no_value_no_component_is_none():
    assert _obs_value({"resourceType": "Observation"}) == (None, None)


# ---- helpers ----

def test_fmt_qty_integral_and_fractional():
    assert _fmt_qty(150.0) == "150"
    assert _fmt_qty(98.6) == "98.6"
    assert _fmt_qty("x") == "x"


def test_norm_bp_unit():
    assert _norm_bp_unit("mm[Hg]") == "mmHg"
    assert _norm_bp_unit("mm Hg") == "mmHg"
    assert _norm_bp_unit(None) == "mmHg"
