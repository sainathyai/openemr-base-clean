"""Layer B: the golden corpus as a versioned Langfuse Dataset + Experiment.

Layer A scores every *production* call. This layer pins a *release* signal: the
21-case hand-authored corpus (13 UC-1 verification-gate cases, 8 UC-3 grounding
cases) is pushed once as a Langfuse Dataset, then replayed as an Experiment on
every run so accuracy is tracked release-over-release in one place.

  Dataset  : clinical-copilot-golden  (one item per case, stable id -> upsert)
  Task     : re-run the case's deterministic evaluate() (stub-only, no Claude spend)
  Item eval: case_passed (1/0)
  Run eval : verification_pass_rate, grounded_rate, corpus_pass_rate

Run name carries the corpus version + a UTC timestamp so two runs compare cleanly
in the Langfuse UI. Fully offline against the model: the corpus is deterministic,
so this is safe to run in CI and costs nothing at the LLM.

    python -m eval.langfuse_experiment            # push dataset + run experiment
    python -m eval.langfuse_experiment --sync     # only (re)sync the dataset
    python -m eval.langfuse_experiment --local     # no Langfuse: print the scorecard

Requires LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST (or
LANGFUSE_BASE_URL) in the environment / .env.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from app import trace  # reuses the guarded client + env-alias handling

DATASET = "clinical-copilot-golden"
CORPUS_VERSION = "1.0"


# ---------- corpus <-> dataset ----------

def _all_cases():
    from eval.cases import GATE_CASES, GROUNDING_CASES
    return ([("gate", c) for c in GATE_CASES]
            + [("grounding", c) for c in GROUNDING_CASES])


def _item_id(suite: str, case_id: str) -> str:
    return f"{suite}:{case_id}"


# case lookup for the task; built once
_INDEX = {c.id: (suite, c) for suite, c in _all_cases()}


def sync_dataset(client) -> int:
    """Idempotently create the dataset and one item per case. Stable ids make this
    an upsert, so re-running never duplicates."""
    client.create_dataset(
        name=DATASET,
        description="Clinical Co-Pilot golden eval: UC-1 fail-closed verification "
                    "and UC-3 flag-but-show grounding cases.",
        metadata={"corpus_version": CORPUS_VERSION, "suites": ["gate", "grounding"]},
    )
    n = 0
    for suite, c in _all_cases():
        client.create_dataset_item(
            dataset_name=DATASET,
            id=_item_id(suite, c.id),
            input={"id": c.id, "suite": suite, "intent": c.intent},
            expected_output={"passed": True},
            metadata={"suite": suite},
        )
        n += 1
    client.flush()
    return n


# ---------- task + evaluators ----------

def _task(*, item, **_):
    data = item["input"] if isinstance(item, dict) else item.input
    cid = (data or {}).get("id")
    entry = _INDEX.get(cid)
    if entry is None:
        return {"passed": False, "suite": (data or {}).get("suite", "?"),
                "intent": "(unknown case)", "failed": [f"no case for id {cid!r}"]}
    suite, case = entry
    report = case.evaluate()
    failed = [f"{r.name}: {r.detail}" for r in report.rubrics if not r.passed]
    return {"passed": report.passed, "suite": report.suite,
            "intent": report.intent, "failed": failed}


def _eval_case_passed(*, input, output, expected_output=None, metadata=None, **_):
    from langfuse.experiment import Evaluation
    passed = bool((output or {}).get("passed"))
    comment = "ok" if passed else "; ".join((output or {}).get("failed", [])) or "failed"
    return Evaluation(name="case_passed", value=1.0 if passed else 0.0,
                      data_type="NUMERIC", comment=comment)


def _run_evaluators(*, item_results, **_):
    from langfuse.experiment import Evaluation
    by = defaultdict(lambda: [0, 0])   # suite -> [passed, total]
    total = [0, 0]
    for r in item_results:
        out = getattr(r, "output", None) or {}
        suite = out.get("suite", "?")
        p = 1 if out.get("passed") else 0
        by[suite][0] += p; by[suite][1] += 1
        total[0] += p; total[1] += 1

    def rate(pair):
        return (pair[0] / pair[1]) if pair[1] else 0.0

    g = by.get("gate", [0, 0])
    gr = by.get("grounding", [0, 0])
    return [
        Evaluation(name="verification_pass_rate", value=rate(g), data_type="NUMERIC",
                   comment=f"{g[0]}/{g[1]} UC-1 gate cases passed"),
        Evaluation(name="grounded_rate", value=rate(gr), data_type="NUMERIC",
                   comment=f"{gr[0]}/{gr[1]} UC-3 grounding cases passed"),
        Evaluation(name="corpus_pass_rate", value=rate(total), data_type="NUMERIC",
                   comment=f"{total[0]}/{total[1]} total cases passed"),
    ]


# ---------- entrypoints ----------

def run_experiment(sync: bool = True):
    client = trace._client()
    if client is None:
        print("Langfuse is not configured (set LANGFUSE_PUBLIC_KEY / "
              "LANGFUSE_SECRET_KEY / LANGFUSE_HOST). Use --local to print the "
              "scorecard without Langfuse.", file=sys.stderr)
        return 2

    if sync:
        n = sync_dataset(client)
        print(f"Synced {n} items into dataset '{DATASET}'.")

    ds = client.get_dataset(DATASET)
    run_name = f"v{CORPUS_VERSION}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    result = ds.run_experiment(
        name=DATASET,
        run_name=run_name,
        description=f"Golden corpus replay, corpus v{CORPUS_VERSION}.",
        task=_task,
        evaluators=[_eval_case_passed],
        run_evaluators=[_run_evaluators],
        metadata={"corpus_version": CORPUS_VERSION},
    )
    client.flush()

    print(f"\nExperiment run: {run_name}")
    for ev in getattr(result, "run_evaluations", []) or []:
        print(f"  {ev.name:24s} {ev.value:.2f}   {ev.comment or ''}")
    url = getattr(result, "dataset_run_url", None)
    if url:
        print(f"\nView run: {url}")
    return 0


def run_local():
    """No-Langfuse fallback: run the same corpus through the harness and print it."""
    from eval.harness import format_scorecard, run_all
    print(format_scorecard(run_all()))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--local" in argv:
        return run_local()
    if "--sync" in argv:
        client = trace._client()
        if client is None:
            print("Langfuse not configured.", file=sys.stderr)
            return 2
        n = sync_dataset(client)
        print(f"Synced {n} items into dataset '{DATASET}'.")
        return 0
    return run_experiment(sync=True)


if __name__ == "__main__":
    raise SystemExit(main())
