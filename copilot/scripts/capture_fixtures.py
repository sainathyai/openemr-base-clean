"""Snapshot the live OpenEMR patients into JSON fixtures for the Path-B demo.

Run ONCE against a running OpenEMR (no Claude spend; FHIR reads only):

    cd copilot
    python -m scripts.capture_fixtures

Writes app/data/fixtures/roster.json and app/data/fixtures/contexts/<uuid>.json.
The deployed Co-Pilot then reads these with COPILOT_FIXTURES set, so it needs no EMR.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.fhir_client import FhirClient

OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "fixtures"


async def main() -> None:
    ctxdir = OUT / "contexts"
    ctxdir.mkdir(parents=True, exist_ok=True)
    client = FhirClient()
    try:
        roster, _ = await client.patients()
        (OUT / "roster.json").write_text(json.dumps(roster, indent=2), encoding="utf-8")
        print(f"roster: {len(roster)} patients")
        for p in roster:
            uuid = p["uuid"]
            ctx = await client.get_context(uuid)
            (ctxdir / f"{uuid}.json").write_text(
                ctx.model_dump_json(indent=2), encoding="utf-8")
            print(f"  captured {p['name']:<40} labs={len(ctx.labs)} "
                  f"meds={len(ctx.medications)} problems={len(ctx.problems)}")
    finally:
        await client.aclose()
    print(f"done -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
