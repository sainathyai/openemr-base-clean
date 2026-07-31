"""UC-3 conversational chart Q&A demo.

Fetches one patient's context once, then answers a set of questions in a single
multi-turn session. Uses Claude if ANTHROPIC_API_KEY is set, else the stub.

Usage:
  python run_chat.py                       # default patient, scripted questions
  python run_chat.py <uuid> "question?"    # one question
"""
from __future__ import annotations

import asyncio
import sys

from app.chat import get_chat
from app.fhir_client import FhirClient

PFEFFER = "a26126ac-06fc-4158-a632-23dbd45b7cfa"

DEFAULT_QUESTIONS = [
    "What's her most recent creatinine and is it abnormal?",
    "What changed since her last visit?",
    "Is she on any blood thinners?",
    "Does she have any documented allergies?",
    "What's her latest hemoglobin A1c?",  # not in the loaded panel -> should decline
]


async def main() -> None:
    uuid = sys.argv[1] if len(sys.argv) > 1 else PFEFFER
    questions = [sys.argv[2]] if len(sys.argv) > 2 else DEFAULT_QUESTIONS

    client = FhirClient()
    try:
        ctx = await client.get_context(uuid)
    finally:
        await client.aclose()

    agent = get_chat(ctx)
    d = ctx.demographics
    print(f"Session for {d.name if d else uuid}  (agent: {agent.name})\n" + "=" * 64)

    for q in questions:
        r = agent.ask(q)
        flag = "OK" if r.grounded else "UNGROUNDED"
        print(f"\nQ: {q}")
        print(f"A: {r.answer}")
        print(f"   [{flag}] tools={r.tool_calls} citations={len(r.citations)} "
              f"latency={r.latency_ms}ms", end="")
        if "input_tokens" in r.usage:
            print(f" tokens=in{r.usage['input_tokens']}/out{r.usage['output_tokens']}", end="")
        print()
        for v in r.violations:
            print(f"   ! {v.rule}: {v.detail}")


if __name__ == "__main__":
    asyncio.run(main())
