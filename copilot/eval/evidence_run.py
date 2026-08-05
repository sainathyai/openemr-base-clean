"""Run the evidence golden eval and print a scorecard. Non-zero exit on failure.

    python -m eval.evidence_run

Deterministic and stub-only (no LLM, no DB, no downloads), so it gates CI.
"""
from __future__ import annotations

import sys

from .evidence_harness import format_scorecard, run_all


def main() -> int:
    sc = run_all()
    print(format_scorecard(sc))
    if not sc.all_passed:
        print(f"\nFAILED: {len(sc.failures())} case(s) did not pass")
        return 1
    print("\nOK: all evidence cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
