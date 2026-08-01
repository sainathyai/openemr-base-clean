"""Golden-eval harness: turn a corpus of hand-authored cases into a scorecard.

This is the systematic complement to the unit tests. The unit tests pin individual
behaviors; this corpus asserts the two gates hold ACROSS many cases and adversarial
phrasings, and yields aggregate rates (verification_pass_rate, grounded_rate) that
mirror the Langfuse scores. It is stub-only and deterministic: no live OpenEMR, no
Claude spend, so it runs in CI on every change.

Two suites:
  - GateCase     : UC-1 fail-closed verification. A SummaryDraft (faithful or
                   fabricated) is run through verify_draft; the rubric asserts which
                   statements survive and which violation rule fires on the rest.
  - GroundingCase: UC-3 flag-but-show grounding. A question is run through StubChat
                   over a fixture PatientContext; the rubric asserts the grounded
                   flag, a required citation, and answer content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.chat import StubChat, check_grounding
from app.schemas import PatientContext
from app.summary import Fact, SummaryDraft, verify_draft
from app.tools import ContextTools


# ---------- rubric primitives ----------

@dataclass
class Rubric:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseReport:
    case_id: str
    suite: str
    intent: str
    rubrics: list[Rubric]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.rubrics)


# ---------- case definitions ----------

@dataclass
class GateCase:
    """A UC-1 verification case. `expect_ok[i]` is whether statement i must survive
    the gate; `expect_rules[i]` is a set of violation rules that must ALL appear on a
    dropped statement (empty for a surviving one)."""
    id: str
    intent: str
    facts: dict[str, Fact]
    draft: SummaryDraft
    expect_ok: list[bool]
    expect_rules: list[set[str]] = field(default_factory=list)

    def evaluate(self) -> CaseReport:
        results = verify_draft(self.draft, self.facts)
        rubrics: list[Rubric] = []
        rubrics.append(Rubric(
            "statement_count",
            len(results) == len(self.expect_ok),
            f"got {len(results)} statements, expected {len(self.expect_ok)}"))
        for i, r in enumerate(results):
            if i >= len(self.expect_ok):
                break
            want_ok = self.expect_ok[i]
            got_rules = {v.rule for v in r.violations}
            rubrics.append(Rubric(
                f"stmt[{i}]_verdict",
                r.ok == want_ok,
                f"ok={r.ok} expected {want_ok}; rules={sorted(got_rules) or 'none'}"))
            if not want_ok and i < len(self.expect_rules) and self.expect_rules[i]:
                want_rules = self.expect_rules[i]
                rubrics.append(Rubric(
                    f"stmt[{i}]_rule",
                    want_rules.issubset(got_rules),
                    f"expected rules {sorted(want_rules)} within {sorted(got_rules)}"))
            # a dropped statement must never leak a rendered line
            if not want_ok:
                rubrics.append(Rubric(
                    f"stmt[{i}]_not_rendered",
                    r.rendered is None,
                    "dropped statement leaked a rendered line" if r.rendered else "ok"))
        return CaseReport(self.id, "gate", self.intent, rubrics)


@dataclass
class GroundingCase:
    """A UC-3 grounding case.

    Faithful path: a `question` is answered by the offline StubChat, which cites only
    what its tools returned, so it should come back grounded.

    Adversarial path: set `inject_answer`/`inject_citations` (and `prime_tool` to fill
    the ledger). The fabricated answer is checked straight through check_grounding, so
    the rubric asserts the grounding gate FLAGS it (grounded=False + the expected rule).
    """
    id: str
    intent: str
    ctx: PatientContext
    question: str = ""
    expect_grounded: bool = True
    expect_citation: Optional[str] = None
    expect_answer_contains: Optional[str] = None
    # adversarial path
    prime_tool: Optional[str] = None
    prime_arg: Optional[str] = None
    inject_answer: Optional[str] = None
    inject_citations: list[str] = field(default_factory=list)
    expect_violation_rule: Optional[str] = None

    def evaluate(self) -> CaseReport:
        if self.inject_answer is not None:
            return self._evaluate_adversarial()
        res = StubChat(self.ctx).ask(self.question)
        rubrics = [Rubric(
            "grounded_flag",
            res.grounded == self.expect_grounded,
            f"grounded={res.grounded} expected {self.expect_grounded}; "
            f"violations={[v.rule for v in res.violations]}")]
        if self.expect_citation is not None:
            rubrics.append(Rubric(
                "required_citation",
                self.expect_citation in res.citations,
                f"{self.expect_citation} not in {res.citations}"))
        if self.expect_answer_contains is not None:
            rubrics.append(Rubric(
                "answer_contains",
                self.expect_answer_contains.lower() in res.answer.lower(),
                f"'{self.expect_answer_contains}' not in answer: {res.answer!r}"))
        return CaseReport(self.id, "grounding", self.intent, rubrics)

    def _evaluate_adversarial(self) -> CaseReport:
        tools = ContextTools(self.ctx)
        if self.prime_tool:
            getattr(tools, self.prime_tool)(*( [self.prime_arg] if self.prime_arg else []))
        violations = check_grounding(self.inject_answer or "", self.inject_citations, tools)
        grounded = len(violations) == 0
        rules = {v.rule for v in violations}
        rubrics = [Rubric(
            "grounded_flag",
            grounded == self.expect_grounded,
            f"grounded={grounded} expected {self.expect_grounded}; rules={sorted(rules)}")]
        if self.expect_violation_rule is not None:
            rubrics.append(Rubric(
                "violation_rule",
                self.expect_violation_rule in rules,
                f"expected '{self.expect_violation_rule}' within {sorted(rules)}"))
        return CaseReport(self.id, "grounding", self.intent, rubrics)


# ---------- scoring ----------

@dataclass
class Scorecard:
    reports: list[CaseReport]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.reports)

    def _suite(self, name: str) -> list[CaseReport]:
        return [r for r in self.reports if r.suite == name]

    def rate(self, suite: str) -> tuple[int, int]:
        rs = self._suite(suite)
        return sum(r.passed for r in rs), len(rs)

    def failures(self) -> list[CaseReport]:
        return [r for r in self.reports if not r.passed]


def run_all() -> Scorecard:
    # imported here to avoid a cycle (cases imports harness types)
    from .cases import GATE_CASES, GROUNDING_CASES
    reports = [c.evaluate() for c in GATE_CASES]
    reports += [c.evaluate() for c in GROUNDING_CASES]
    return Scorecard(reports)


def format_scorecard(sc: Scorecard) -> str:
    lines: list[str] = ["Clinical Co-Pilot golden eval", "=" * 34]
    for suite, label in (("gate", "UC-1 verification gate (fail-closed)"),
                         ("grounding", "UC-3 grounding gate (flag-but-show)")):
        passed, total = sc.rate(suite)
        lines.append(f"\n{label}: {passed}/{total} cases")
        for r in sc._suite(suite):
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{mark}] {r.case_id}: {r.intent}")
            if not r.passed:
                for rub in r.rubrics:
                    if not rub.passed:
                        lines.append(f"          - {rub.name}: {rub.detail}")
    gp, gt = sc.rate("gate")
    rp, rt = sc.rate("grounding")
    lines.append("\n" + "-" * 34)
    lines.append(f"TOTAL: {gp + rp}/{gt + rt} cases passed")
    lines.append("verification_pass_rate: "
                 f"{gp / gt:.2f}" if gt else "verification_pass_rate: n/a")
    lines.append("grounded_rate:          "
                 f"{rp / rt:.2f}" if rt else "grounded_rate: n/a")
    return "\n".join(lines)
