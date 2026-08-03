# Testing the Clinical Co-Pilot

The test suite exists to prove one thing: **the co-pilot never presents a clinical
claim it cannot source and verify.** Every test pins a trust invariant, not just a
happy path. There are two layers, both hermetic and both free to run:

- **Unit/behavior tests** (`tests/`) — 58 tests, pytest.
- **Golden eval corpus** (`eval/`) — a scored, stub-only corpus that measures the two
  gates across many cases and reports a pass *rate*, the way an LLM eval does.

## Principles

1. **Hermetic.** No live OpenEMR, no network, no Claude spend. `conftest.py` blanks the
   Langfuse and Anthropic keys before config loads, so tracing degrades to a no-op and
   nothing flushes to the live observability project. The whole suite runs in ~0.1s,
   which is itself the proof that no call left the process.
2. **Deterministic stubs stand in for the model.** `StubNarrator` / `StubChat` produce
   faithful structured output with no API key, so the graph, the gates, and the tools
   are exercised end to end offline. `COPILOT_FORCE_STUB=1` forces this even when a key
   is present.
3. **Every test asserts an invariant a physician relies on** — a bad claim is dropped, a
   number is sourced, an abnormal is classified correctly, an unsafe order is flagged.
4. **The harness can go red.** The eval corpus ships a negative control: fed a wrong
   expectation, it fails. Green is therefore meaningful.

## Running

```bash
cd copilot
python -m pytest -q          # the 58 unit/behavior tests
python -m eval.run           # the golden corpus scorecard (non-zero exit on failure)
```

`tests/test_eval.py` also runs the corpus inside pytest, so CI catches a gate
regression with the specific case and rubric that broke.

## What each file guards

| File | Count | Invariant under test |
|------|-------|----------------------|
| `tests/test_reference_ranges.py` | 13 | **D-9 lab classification.** Band edges (normal/high/critical), sex-specific ranges (creatinine, hgb, hct), unit mismatch refuses to classify, pediatric fail-safe (under 18 never gets adult ranges), inverted analytes (HDL higher-is-better), target-based lipids. Abnormality is decided here, never by the model. |
| `tests/test_verification.py` | 10 | **The UC-1 gate (fails closed).** Faithful claims render; a claim citing an unknown source, asserting the wrong direction, inventing a number, upgrading to a fabricated critical, or mismatching its category is dropped with the expected violation rule. A full-hallucination draft yields nothing. |
| `tests/test_chat.py` | 7 | **The UC-3 grounding gate (flags, shows).** Faithful answers come back grounded and cited; a citation or number no tool returned is flagged as ungrounded; an absent lab is answered honestly rather than invented. |
| `tests/test_fhir_extraction.py` | 10 | **Evidence extraction at the FHIR boundary.** Blood pressure from `component[]` renders as `systolic/diastolic` (DQ-5), trailing `.0` is dropped, both numbers surface for grounding, and the DQ-6 `{entry.value}` placeholder is scrubbed rather than shown as data. |
| `tests/test_order_safety.py` | 13 | **UC-2 order safety.** See below. |
| `tests/test_eval.py` | 5 | **CI guard for the golden corpus.** All cases pass, both headline rates are perfect on the deterministic path, minimum coverage is met, and tracing is confirmed off during tests. |

## UC-2 order-safety tests (`tests/test_order_safety.py`)

The order-safety engine (`app/order_safety.py`) is deterministic and sourced, so its
tests build synthetic `PatientContext`s and pin each safety axis independently. No
network; a helper constructs the patient inline.

**Allergy**
- `test_direct_allergy_is_danger_and_sourced` — a documented penicillin allergy makes
  an amoxicillin order **danger**, and the finding cites the `AllergyIntolerance` id.
- `test_penicillin_allergy_cross_reacts_with_cephalosporin_as_caution` — a penicillin
  allergy raises a **caution** (not danger) on cephalexin via class cross-reactivity.

**Renal** (the drug's renal profile against the patient's own labs)
- `test_nsaid_in_ckd_with_high_creatinine_is_danger` — ibuprofen with a high creatinine
  and a CKD problem fires both the renal lab flag and the problem-list contraindication.
- `test_renal_drug_with_normal_creatinine_is_info` — lisinopril with a normal creatinine
  is downgraded to an **info** note, not a warning.
- `test_renal_drug_without_labs_flags_unknown` — a renally-handled drug with no recent
  creatinine/eGFR is a **caution** ("renal function not on file"), never silent.

**Duplicate therapy + bleeding**
- `test_same_class_is_duplicate` — ordering aspirin while on clopidogrel (both
  antiplatelet) is a duplicate-therapy caution citing the existing med.
- `test_cross_group_additive_bleeding_risk` — aspirin plus existing warfarin raises an
  additive-bleeding caution.

**Indication / contraindication from the problem list**
- `test_indication_from_problem_list_is_info` — lisinopril is marked appropriate for an
  active hypertension problem, citing the `Condition` id.
- `test_metformin_contraindicated_in_ckd` — metformin against a CKD problem is **danger**.
- `test_historical_problem_is_not_a_contraindication` — **regression test.** "Past
  pregnancy history of miscarriage" must NOT trigger the ACE-inhibitor pregnancy
  contraindication. Naive substring matching regressed on exactly this real Synthea
  entry; the fix is whole-word matching plus a guard that skips historical/contextual
  problems.

**Fail-safe + provenance**
- `test_unrecognized_drug_is_not_cleared` — a drug not in the knowledge table returns
  "not recognized" with **no** clearance. Silence is never treated as safety.
- `test_clean_order_reports_clear` — a genuinely unremarkable order reports a single
  `ok` "no safety flags" finding.
- `test_every_finding_on_a_real_order_is_sourced_or_advisory` — every finding that
  asserts a chart fact carries a `source_id`; only advisory notes (e.g. "renal function
  unknown") may be unsourced.

## The golden eval corpus (`eval/`)

Distinct from the unit tests: the corpus is a set of hand-authored cases scored to two
headline rates that mirror the Langfuse scores — `verification_pass_rate` (UC-1) and
`grounded_rate` (UC-3). It answers "do the gates hold across a corpus?" rather than
"does this one function work?" 21 cases (13 gate + 8 grounding), currently 21/21 with
both rates at 1.00 on the deterministic path. The negative control proves the corpus
can fail, so the perfect score is not vacuous.
