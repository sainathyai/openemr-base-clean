"""CI guard for the golden eval corpus (eval/). Stub-only, no Claude spend.

Every hand-authored case must pass, and the aggregate rates that mirror the Langfuse
scores must be perfect on the deterministic path. If a gate regresses, this fails
with the specific case and rubric that broke.
"""
from __future__ import annotations

import pytest

from eval.harness import run_all


def test_golden_corpus_all_pass():
    sc = run_all()
    failures = sc.failures()
    assert not failures, "golden eval failures:\n" + "\n".join(
        f"  {r.case_id}: " + "; ".join(f"{rub.name}={rub.detail}"
                                       for rub in r.rubrics if not rub.passed)
        for r in failures)


def test_headline_rates_are_perfect():
    sc = run_all()
    gp, gt = sc.rate("gate")
    rp, rt = sc.rate("grounding")
    assert gt and rt, "corpus is empty"
    assert gp == gt, f"verification_pass_rate {gp}/{gt}"
    assert rp == rt, f"grounded_rate {rp}/{rt}"


@pytest.mark.parametrize("suite,minimum", [("gate", 10), ("grounding", 6)])
def test_corpus_has_meaningful_coverage(suite, minimum):
    sc = run_all()
    _, total = sc.rate(suite)
    assert total >= minimum, f"{suite} suite only has {total} cases"


def test_tracing_is_silenced_during_tests():
    """conftest.py must keep observability off so the suite never flushes synthetic
    traces into the live Langfuse project (and never spends Claude credits)."""
    from app import trace
    assert trace.enabled() is False
