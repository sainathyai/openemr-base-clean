"""CI guard for the 50-case evidence golden eval.

Asserts the corpus is complete and fully green, and includes negative controls
proving the harness goes RED when a case's expectation is wrong -- so a passing
run is meaningful and a weakened gate/retriever would be caught.
"""
from eval.evidence_cases import EXTRACTION_CASES, RETRIEVAL_CASES
from eval.evidence_harness import ExtractionCase, RetrievalCase, run_all


def test_corpus_size_is_fifty():
    assert len(EXTRACTION_CASES) == 30
    assert len(RETRIEVAL_CASES) == 20


def test_all_cases_pass():
    sc = run_all()
    assert sc.all_passed, "\n".join(
        f"{r.case_id}: {[rb.detail for rb in r.rubrics if not rb.passed]}"
        for r in sc.failures()
    )
    assert sc.rate("extraction") == (30, 30)
    assert sc.rate("retrieval") == (20, 20)


def test_negative_control_extraction_expectation_can_fail():
    # claim a fabricated analyte should be KEPT -> harness must report FAIL
    bad = ExtractionCase(
        "neg", "fabricated value wrongly expected kept", "lab",
        "Troponin 9.99", expect_kept=True, value="9.99", analyte="Troponin",
    )
    assert bad.evaluate().passed is False


def test_negative_control_retrieval_expectation_can_fail():
    # demand an unrelated guideline for a metformin query -> must FAIL
    bad = RetrievalCase(
        "neg", "wrong expected id", "metformin dosing low eGFR",
        expect_id="accaha-statin-secondary", k=3,
    )
    assert bad.evaluate().passed is False


def test_gate_still_drops_when_expectation_flips():
    # a truly grounded value expected kept must PASS (sanity of the positive path)
    good = ExtractionCase(
        "pos", "grounded value", "lab", "Creatinine 2.10",
        expect_kept=True, value="2.10", analyte="Creatinine",
    )
    assert good.evaluate().passed is True
