"""The 50-case evidence golden corpus.

30 extraction cases exercise the gate's grounded-or-dropped invariant across
faithful reads, misread decimals, values borrowed from the wrong row, fabricated
analytes/meds, and fabricated span text. 20 retrieval cases assert the hybrid
RAG surfaces the right cited guideline for a clinical query. Boolean rubrics,
run in CI; if the gate or fusion is weakened, the relevant cases go red.
"""
from __future__ import annotations

from .evidence_harness import ExtractionCase, RetrievalCase

# --------------------------------------------------------------------------- #
# Extraction gate (30)
# --------------------------------------------------------------------------- #
EXTRACTION_CASES: list[ExtractionCase] = [
    # -- faithful labs: kept --
    ExtractionCase("xl01", "faithful creatinine", "lab", "Creatinine 2.10", True, value="2.10", unit="mg/dL", analyte="Creatinine"),
    ExtractionCase("xl02", "faithful eGFR", "lab", "eGFR 38", True, value="38", unit="mL/min/1.73", analyte="eGFR"),
    ExtractionCase("xl03", "faithful potassium", "lab", "Potassium 5.1", True, value="5.1", unit="mmol/L", analyte="Potassium"),
    ExtractionCase("xl04", "faithful sodium", "lab", "Sodium 139", True, value="139", unit="mmol/L", analyte="Sodium"),
    ExtractionCase("xl05", "faithful glucose", "lab", "Glucose 142", True, value="142", unit="mg/dL", analyte="Glucose"),
    ExtractionCase("xl06", "faithful hemoglobin", "lab", "Hemoglobin 10.8", True, value="10.8", unit="g/dL", analyte="Hemoglobin"),
    ExtractionCase("xl07", "faithful hematocrit", "lab", "Hematocrit 33.1", True, value="33.1", unit="%", analyte="Hematocrit"),
    ExtractionCase("xl08", "value with unit in span", "lab", "2.10 mg/dL", True, value="2.10", unit="mg/dL", analyte="Creatinine"),
    # -- misread decimals: value not in its span -> dropped --
    ExtractionCase("xl09", "misread creatinine decimal", "lab", "Creatinine 2.10", False, value="21.0", analyte="Creatinine", expect_reason="value not present"),
    ExtractionCase("xl10", "misread glucose decimal", "lab", "Glucose 142", False, value="14.2", analyte="Glucose", expect_reason="value not present"),
    ExtractionCase("xl11", "misread hemoglobin decimal", "lab", "Hemoglobin 10.8", False, value="1.08", analyte="Hemoglobin", expect_reason="value not present"),
    ExtractionCase("xl12", "misread sodium", "lab", "Sodium 139", False, value="13.9", analyte="Sodium", expect_reason="value not present"),
    # -- value borrowed from a different row -> dropped --
    ExtractionCase("xl13", "potassium row with sodium value", "lab", "Potassium 5.1", False, value="139", analyte="Potassium", expect_reason="value not present"),
    ExtractionCase("xl14", "creatinine row with egfr value", "lab", "Creatinine 2.10", False, value="38", analyte="Creatinine", expect_reason="value not present"),
    # -- fabricated analytes absent from the report -> dropped --
    ExtractionCase("xl15", "fabricated troponin", "lab", "Troponin 9.99", False, value="9.99", analyte="Troponin", expect_reason="not found"),
    ExtractionCase("xl16", "fabricated BNP", "lab", "BNP 1200", False, value="1200", analyte="BNP", expect_reason="not found"),
    ExtractionCase("xl17", "fabricated lactate", "lab", "Lactate 4.0", False, value="4.0", analyte="Lactate", expect_reason="not found"),
    ExtractionCase("xl18", "fabricated calcium", "lab", "Calcium 8.0", False, value="8.0", analyte="Calcium", expect_reason="not found"),
    ExtractionCase("xl19", "fabricated ferritin", "lab", "Ferritin 5", False, value="5", analyte="Ferritin", expect_reason="not found"),
    # -- fabricated span text (extra token never printed) -> dropped --
    ExtractionCase("xl20", "span with invented word", "lab", "Creatinine 2.10 critical", False, value="2.10", analyte="Creatinine", expect_reason="not found"),

    # -- faithful intake statements: kept --
    ExtractionCase("xi01", "faithful med lisinopril", "statement", "lisinopril 10 mg once daily", True, stmt_kind="medication"),
    ExtractionCase("xi02", "faithful med atorvastatin", "statement", "atorvastatin 40 mg at bedtime", True, stmt_kind="medication"),
    ExtractionCase("xi03", "faithful med metformin", "statement", "metformin 1000 mg twice daily", True, stmt_kind="medication"),
    ExtractionCase("xi04", "faithful allergy penicillin", "statement", "Penicillin - rash", True, stmt_kind="allergy"),
    ExtractionCase("xi05", "faithful allergy sulfa", "statement", "Sulfa - hives", True, stmt_kind="allergy"),
    ExtractionCase("xi06", "faithful problem CKD", "statement", "Chronic kidney disease", True, stmt_kind="problem"),
    ExtractionCase("xi07", "faithful problem HTN", "statement", "Hypertension", True, stmt_kind="problem"),
    # -- fabricated / misread intake statements: dropped --
    ExtractionCase("xi08", "fabricated med warfarin", "statement", "warfarin 5 mg daily", False, stmt_kind="medication", expect_reason="not found"),
    ExtractionCase("xi09", "wrong lisinopril dose", "statement", "lisinopril 20 mg once daily", False, stmt_kind="medication", expect_reason="not found"),
    ExtractionCase("xi10", "fabricated latex allergy", "statement", "Latex allergy", False, stmt_kind="allergy", expect_reason="not found"),
]

# --------------------------------------------------------------------------- #
# Hybrid RAG relevance (20)
# --------------------------------------------------------------------------- #
RETRIEVAL_CASES: list[RetrievalCase] = [
    RetrievalCase("xr01", "metformin at low eGFR", "metformin dosing continue discontinue low eGFR chronic kidney disease", "kdigo-ckd-metformin"),
    RetrievalCase("xr02", "NSAID avoidance in CKD", "is ibuprofen NSAID safe in chronic kidney disease nephrotoxin", "nsaid-ckd-avoid"),
    RetrievalCase("xr03", "potassium monitoring on RAAS", "potassium monitoring hyperkalemia on ACE inhibitor ARB in CKD", "kdigo-ckd-potassium"),
    RetrievalCase("xr04", "ACE/ARB in albuminuric CKD", "ACE inhibitor angiotensin receptor blocker albuminuria CKD titrate", "kdigo-ckd-ace-arb"),
    RetrievalCase("xr05", "CKD staging by eGFR", "chronic kidney disease staging eGFR G3b moderately reduced", "kdigo-ckd-staging"),
    RetrievalCase("xr06", "anemia of CKD", "anemia chronic kidney disease hemoglobin iron studies erythropoiesis", "kdigo-ckd-anemia"),
    RetrievalCase("xr07", "A1c target in T2DM", "HbA1c glycemic target goal type 2 diabetes below 7 percent", "ada-t2dm-a1c-target"),
    RetrievalCase("xr08", "SGLT2 first-line with CKD", "SGLT2 inhibitor first-line pharmacotherapy diabetes kidney benefit", "ada-t2dm-first-line"),
    RetrievalCase("xr09", "BP target in hypertension", "blood pressure target goal hypertension 130 80 intensification", "accaha-htn-target"),
    RetrievalCase("xr10", "statin monitoring", "high-intensity statin atorvastatin lipid panel monitoring ASCVD", "accaha-statin-secondary"),
    RetrievalCase("xr11", "penicillin cephalosporin cross", "penicillin allergy cephalosporin cross reactivity side chains", "penicillin-cephalosporin-cross"),
    RetrievalCase("xr12", "CKD monitoring cadence", "how often assess eGFR albuminuria monitoring cadence CKD stage G3b every 6 months", "uspstf-ckd-monitoring"),
    RetrievalCase("xr13", "medication reconciliation", "medication reconciliation care transition intake home medications discrepancies", "medrec-transitions"),
    RetrievalCase("xr14", "acute hyperkalemia", "serum potassium above 6.0 ECG changes medical emergency hyperkalemia", "hyperkalemia-acute"),
    RetrievalCase("xr15", "creatinine/eGFR relationship", "interpret creatinine 2.10 rising falling eGFR opposite directions", "egfr-creatinine-relationship"),
    RetrievalCase("xr16", "annual kidney screening", "annual urinary albumin creatinine ratio diabetes screening care gap", "ada-ckd-screening"),
    RetrievalCase("xr17", "lactic acidosis risk", "metformin lactic acidosis discontinue eGFR below 30", "kdigo-ckd-metformin"),
    RetrievalCase("xr18", "nephrotoxin acute kidney injury", "avoid nephrotoxic NSAID ibuprofen acute kidney injury renal perfusion", "nsaid-ckd-avoid"),
    RetrievalCase("xr19", "second-line kidney protection", "reduce progression kidney disease cardiovascular SGLT2 independent of A1c", "ada-t2dm-first-line"),
    RetrievalCase("xr20", "statin high intensity dose", "atorvastatin 40 to 80 mg high intensity clinical atherosclerotic disease", "accaha-statin-secondary"),
]
