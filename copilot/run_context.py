"""Pull one patient's full context in parallel and print it. Proves the D-7
data-access foundation against the live OpenEMR before any LLM is added.

Usage: python run_context.py [patient_uuid]
Default patient: Pfeffer (pid 2), the data-rich demo patient.
"""
from __future__ import annotations

import asyncio
import sys
import time

from app.fhir_client import FhirClient

PFEFFER = "a26126ac-06fc-4158-a632-23dbd45b7cfa"


async def main() -> None:
    uuid = sys.argv[1] if len(sys.argv) > 1 else PFEFFER
    client = FhirClient()
    try:
        t0 = time.perf_counter()
        ctx = await client.get_context(uuid)
        wall = int((time.perf_counter() - t0) * 1000)
    finally:
        await client.aclose()

    d = ctx.demographics
    print(f"\nPatient: {d.name if d else '(none)'}  "
          f"DOB={d.birth_date if d else '?'}  sex={d.sex if d else '?'}")
    print(f"  problems={len(ctx.problems)}  meds={len(ctx.medications)}  "
          f"labs={len(ctx.labs)}  vitals={len(ctx.vitals)}  "
          f"encounters={len(ctx.encounters)}  allergies={len(ctx.allergies)}")

    print("\nPer-resource fetch latency (parallel):")
    for k, ms in ctx.fetch_ms.items():
        print(f"  {k:12} {ms:5} ms")
    print(f"  {'WALL':12} {wall:5} ms   (serial would be ~{sum(ctx.fetch_ms.values())} ms)")

    if ctx.warnings:
        print("\nWarnings:")
        for w in ctx.warnings:
            print(f"  ! {w}")

    print("\nSample meds (with source ids):")
    for m in ctx.medications[:5]:
        print(f"  - {m.text}  [{m.source_id}]")
    print("\nMost recent vitals (source-attributed):")
    for v in sorted(ctx.vitals, key=lambda x: (x.effective or _min()), reverse=True)[:6]:
        print(f"  - {v.name} = {v.value} {v.unit or ''}  @ {v.effective}  [{v.source_id}]")


def _min():
    from datetime import datetime, timezone
    return datetime.min.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    asyncio.run(main())
