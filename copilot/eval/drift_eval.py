"""Layer D: periodic, budgeted drift eval against the REAL model.

Layer B replays the golden corpus stub-only: it proves the deterministic gate logic
holds, but never exercises the LLM. Production runs `COPILOT_FORCE_STUB=1` (zero
spend), so the live model is never watched. This layer closes that gap: on a
schedule it runs UC-1 through the *real* narrator (Haiku, cheap) on a small,
budget-capped set of patients and records how the actual model behaves against the
verification gate. If a future model/prompt change starts producing claims the gate
has to drop, the hallucination_rate here moves off zero and the drift run goes red.

Design:
  Dataset  : clinical-copilot-drift   (one item per sampled patient)
  Task     : run UC-1 with ClaudeNarrator(Haiku); read the gate's own counts
  Item eval: verification_pass_rate, hallucination_rate, drift_ok (<= threshold)
  Run eval : mean_verification_pass_rate, mean_hallucination_rate, drift_pass_rate,
             total_input_tokens, total_output_tokens

Budget: hard-capped by DRIFT_MAX_PATIENTS (default 3) and run serially
(max_concurrency=1). Haiku by default. A full run is a handful of cheap calls.

    python -m eval.drift_eval                 # real Haiku drift run (needs ANTHROPIC_API_KEY)
    python -m eval.drift_eval --patients 2    # tighter budget
    python -m eval.drift_eval --model claude-haiku-4-5-20251001
    python -m eval.drift_eval --stub          # zero-cost wiring check (no model call)

Scheduling (off the hot path, budget-capped):
  cron  :  0 8 * * 1  cd /path/copilot && .venv/bin/python -m eval.drift_eval >> drift.log 2>&1
  CI    :  a weekly GitHub Actions job running the same command with the two keys
           (ANTHROPIC_API_KEY, LANGFUSE_*) as secrets.
Do NOT run it on the hot request path — it is a background quality probe, not a
per-request check (that is Layer A).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from app import trace

DRIFT_DATASET = "clinical-copilot-drift"
CORPUS_VERSION = "1.0"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # cheap by design; conserve budget
HALLUCINATION_THRESHOLD = 0.05                # drift_ok if <= this


# ---------- patient sample ----------

def _sample_patients(limit: int) -> list[dict]:
    """Deterministic first-N of the fixture roster, so runs compare like-for-like."""
    import json
    from pathlib import Path
    root = os.getenv("COPILOT_FIXTURES") or "app/data/fixtures"
    roster = json.loads((Path(root) / "roster.json").read_text(encoding="utf-8"))
    return roster[:limit]


# ---------- the real-model run ----------

async def _run_one_async(uuid: str, model: str, stub: bool) -> dict:
    """Run UC-1 once and read the verification gate's own counts. Real model unless
    --stub. Returns metrics; never raises (a failed run is itself a drift signal)."""
    from app.agent import run as run_uc1, verification_stats
    try:
        if stub:
            from app.llm import StubNarrator
            narrator = StubNarrator()
        else:
            from app.llm import ClaudeNarrator
            narrator = ClaudeNarrator(model=model)
        out = await run_uc1(uuid, narrator=narrator)
        stats = verification_stats(out.get("verified", []))
        claims = stats["claims"]
        usage = out.get("usage", {}) or {}
        return {
            "ok": True,
            "claims": claims,
            "passed": stats["passed"],
            "dropped": stats["dropped"],
            "verification_pass_rate": (stats["passed"] / claims) if claims else 1.0,
            "hallucination_rate": (stats["dropped"] / claims) if claims else 0.0,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "provider": usage.get("provider", "?"),
            "dropped_claims": [w for w in out.get("warnings", []) if "dropped" in w.lower()],
        }
    except Exception as e:  # a crash IS drift; record it rather than aborting the run
        return {"ok": False, "error": repr(e), "claims": 0, "passed": 0, "dropped": 0,
                "verification_pass_rate": 0.0, "hallucination_rate": 1.0,
                "input_tokens": 0, "output_tokens": 0, "provider": "error"}


def _run_one(uuid: str, model: str, stub: bool) -> dict:
    """Sync wrapper for the no-event-loop local path (no Langfuse)."""
    return asyncio.run(_run_one_async(uuid, model, stub))


# ---------- Langfuse experiment glue ----------

def _make_task(model: str, stub: bool):
    async def _task(*, item, **_):
        data = item["input"] if isinstance(item, dict) else item.input
        return await _run_one_async((data or {}).get("uuid"), model, stub)
    return _task


def _item_evaluators(*, input, output, expected_output=None, metadata=None, **_):
    from langfuse.experiment import Evaluation
    out = output or {}
    hr = float(out.get("hallucination_rate", 1.0))
    vpr = float(out.get("verification_pass_rate", 0.0))
    dropped = out.get("dropped_claims") or []
    ok = out.get("ok", False) and hr <= HALLUCINATION_THRESHOLD
    return [
        Evaluation(name="verification_pass_rate", value=vpr, data_type="NUMERIC",
                   comment=f"{out.get('passed',0)}/{out.get('claims',0)} claims verified "
                           f"({out.get('provider','?')})"),
        Evaluation(name="hallucination_rate", value=hr, data_type="NUMERIC",
                   comment=("; ".join(dropped)[:400] or "none dropped")
                           if out.get("ok") else f"run failed: {out.get('error','?')}"),
        Evaluation(name="drift_ok", value=1.0 if ok else 0.0, data_type="NUMERIC",
                   comment=f"hallucination_rate {hr:.2f} vs threshold {HALLUCINATION_THRESHOLD}"),
    ]


def _run_evaluators(*, item_results, **_):
    from langfuse.experiment import Evaluation
    n = len(item_results) or 1
    vprs, hrs, tin, tout, drift_pass = [], [], 0, 0, 0
    for r in item_results:
        out = getattr(r, "output", None) or {}
        vprs.append(float(out.get("verification_pass_rate", 0.0)))
        hrs.append(float(out.get("hallucination_rate", 1.0)))
        tin += int(out.get("input_tokens", 0))
        tout += int(out.get("output_tokens", 0))
        if out.get("ok") and float(out.get("hallucination_rate", 1.0)) <= HALLUCINATION_THRESHOLD:
            drift_pass += 1
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return [
        Evaluation(name="mean_verification_pass_rate", value=mean(vprs), data_type="NUMERIC",
                   comment=f"across {len(item_results)} patients"),
        Evaluation(name="mean_hallucination_rate", value=mean(hrs), data_type="NUMERIC",
                   comment="0.0 = model stayed inside the gate"),
        Evaluation(name="drift_pass_rate", value=drift_pass / n, data_type="NUMERIC",
                   comment=f"{drift_pass}/{len(item_results)} patients within threshold"),
        Evaluation(name="total_input_tokens", value=float(tin), data_type="NUMERIC"),
        Evaluation(name="total_output_tokens", value=float(tout), data_type="NUMERIC"),
    ]


def sync_dataset(client, patients: list[dict]) -> int:
    client.create_dataset(
        name=DRIFT_DATASET,
        description="Layer D drift probe: run UC-1 through the real model on a small "
                    "budget-capped patient sample and watch the verification gate.",
        metadata={"corpus_version": CORPUS_VERSION},
    )
    for p in patients:
        client.create_dataset_item(
            dataset_name=DRIFT_DATASET,
            id=f"patient:{p['uuid']}",
            input={"uuid": p["uuid"], "name": p.get("name")},
            expected_output={"max_hallucination_rate": HALLUCINATION_THRESHOLD},
            metadata={"sex": p.get("sex")},
        )
    client.flush()
    return len(patients)


def run(patients_n: int, model: str, stub: bool) -> int:
    patients = _sample_patients(patients_n)
    if not stub and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is required for a real drift run. Use --stub to "
              "check the wiring with zero spend.", file=sys.stderr)
        return 2

    client = trace._client()
    tag = "stub" if stub else model.split("-2")[0]
    run_name = f"v{CORPUS_VERSION}-drift-{tag}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

    if client is None:
        # No Langfuse: still run the probe locally and print a verdict.
        print(f"Langfuse not configured; running {len(patients)} patient(s) locally "
              f"({'stub' if stub else model}).")
        results = [_run_one(p["uuid"], model, stub) for p in patients]
        _print_local(results)
        return 0

    n = sync_dataset(client, patients)
    print(f"Synced {n} patients into dataset '{DRIFT_DATASET}'. "
          f"Running {'stub' if stub else model} drift probe (serial)...")
    ds = client.get_dataset(DRIFT_DATASET)
    result = ds.run_experiment(
        name=DRIFT_DATASET,
        run_name=run_name,
        description=f"Drift probe, {'stub' if stub else model}, corpus v{CORPUS_VERSION}.",
        task=_make_task(model, stub),
        evaluators=[_item_evaluators],
        run_evaluators=[_run_evaluators],
        max_concurrency=1,          # budget/rate control
        metadata={"model": "stub" if stub else model, "corpus_version": CORPUS_VERSION},
    )
    client.flush()

    print(f"\nDrift run: {run_name}")
    for ev in getattr(result, "run_evaluations", []) or []:
        print(f"  {ev.name:28s} {ev.value:>10.2f}   {ev.comment or ''}")
    url = getattr(result, "dataset_run_url", None)
    if url:
        print(f"\nView run: {url}")
    return 0


def _print_local(results: list[dict]) -> None:
    for r in results:
        status = "ok" if r.get("ok") else f"FAIL {r.get('error','')}"
        print(f"  vpr={r['verification_pass_rate']:.2f} "
              f"halluc={r['hallucination_rate']:.2f} "
              f"claims={r['claims']} tok={r['input_tokens']}/{r['output_tokens']} {status}")
    hrs = [r["hallucination_rate"] for r in results]
    print(f"\nmean hallucination_rate: {sum(hrs)/len(hrs):.2f} "
          f"(threshold {HALLUCINATION_THRESHOLD})")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Layer D budgeted drift eval.")
    ap.add_argument("--patients", type=int,
                    default=int(os.getenv("DRIFT_MAX_PATIENTS", "3")),
                    help="how many patients to sample (budget cap)")
    ap.add_argument("--model", default=os.getenv("DRIFT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--stub", action="store_true",
                    help="run the deterministic stub (zero cost) to check wiring")
    args = ap.parse_args(argv)
    return run(args.patients, args.model, args.stub)


if __name__ == "__main__":
    raise SystemExit(main())
