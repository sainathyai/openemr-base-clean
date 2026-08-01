"""Run the golden eval corpus and print a scorecard.

    python -m eval.run          # from the copilot/ directory

Stub-only and deterministic: no live OpenEMR, no Claude spend. Exits non-zero if any
case fails, so it can gate CI. The two headline rates mirror the Langfuse scores
(`verification_pass_rate`, `grounded`) the live agent emits per run.
"""
from __future__ import annotations

import sys

from .harness import format_scorecard, run_all


def main() -> int:
    sc = run_all()
    print(format_scorecard(sc))
    return 0 if sc.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
