"""Run the UC-1 pre-visit synthesis agent for one patient and print the briefing
plus the full observability trace.

Usage: python run_agent.py [patient_uuid]
Uses Claude if ANTHROPIC_API_KEY is set, otherwise the deterministic stub narrator.
"""
from __future__ import annotations

import asyncio
import sys

from app.agent import run, verification_stats
from app.llm import get_narrator

PFEFFER = "a26126ac-06fc-4158-a632-23dbd45b7cfa"


async def main() -> None:
    uuid = sys.argv[1] if len(sys.argv) > 1 else PFEFFER
    narrator = get_narrator()
    out = await run(uuid, narrator)

    print("\n" + out["summary_md"])

    stats = verification_stats(out["verified"])
    print("\n" + "=" * 60)
    print(f"correlation_id : {out['correlation_id']}")
    print(f"narrator       : {narrator.name}")
    print(f"verification   : {stats['passed']}/{stats['claims']} claims passed, "
          f"{stats['dropped']} dropped")
    u = out.get("usage", {})
    if "input_tokens" in u:
        print(f"tokens         : in={u['input_tokens']} out={u['output_tokens']} "
              f"stop={u.get('stop_reason')}")
    print(f"timings_ms     : {out['timings_ms']}")
    print(f"fetch_ms       : {out.get('fetch_ms', {})}")
    if out.get("warnings"):
        print("\nwarnings / dropped claims:")
        for w in out["warnings"]:
            print(f"  ! {w}")


if __name__ == "__main__":
    asyncio.run(main())
