"""Narrator providers: the swappable LLM step.

The graph and the verification gate are identical regardless of which narrator
runs. `StubNarrator` is a faithful, deterministic narrator that needs no API key
(used for offline runs, CI, and eval baselines). `ClaudeNarrator` calls Claude
with forced structured tool-use so the model can only return a SummaryDraft.

Whichever runs, its output is untrusted and must pass verify_draft() before any
statement reaches the physician.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from .changes import ChangeSet
from .summary import Fact, SummaryDraft, Statement, build_facts


class Narrator(Protocol):
    name: str

    def narrate(self, cs: ChangeSet, facts: dict[str, Fact]) -> tuple[SummaryDraft, dict]:
        """Return (draft, usage_metadata)."""
        ...


# ---------- deterministic, no-key narrator ----------

class StubNarrator:
    name = "stub"

    def narrate(self, cs: ChangeSet, facts: dict[str, Fact]) -> tuple[SummaryDraft, dict]:
        st: list[Statement] = []
        for a in cs.current_criticals:
            st.append(Statement(category="critical", asserted_status=a.status,
                                 source_ids=[a.source_id],
                                 text=f"{_short(a.name)} is at a critical level"))
        for a in cs.current_abnormals:
            if a.is_critical:
                continue
            side = "elevated" if "high" in a.status else "low"
            st.append(Statement(category="abnormal_lab", asserted_status=a.status,
                                 source_ids=[a.source_id],
                                 text=f"{_short(a.name)} is {side}"))
        for t in cs.notable_trends:
            st.append(Statement(category="trend", asserted_status=t.flag,
                                 source_ids=[t.latest_source_id],
                                 text=f"{_short(t.name)} is {t.flag.replace('_', ' ')}"))
        for m in cs.new_medications:
            st.append(Statement(category="new_medication", source_ids=[m.source_id],
                                 text=f"New medication: {m.text}"))
        for p in cs.new_problems:
            st.append(Statement(category="new_problem", source_ids=[p.source_id],
                                 text=f"New problem recorded: {p.text}"))
        headline = f"Pre-visit summary for {cs.patient_name or cs.patient_id}"
        return SummaryDraft(headline=headline, statements=st), {"provider": "stub"}


def _short(name: str) -> str:
    return name.split("[")[0].strip()


# ---------- Claude narrator ----------

_SYSTEM = """You are a clinical pre-visit summarizer for a primary-care physician.
You receive a JSON change-set of facts that have ALREADY been computed and verified
by a deterministic engine (lab abnormality, trends, new meds, new problems). Your job
is ONLY to phrase these facts as a concise, scannable pre-visit briefing.

Hard rules:
- Talk ONLY about facts present in the change-set. Never introduce a clinical claim,
  diagnosis, or recommendation that is not in the data.
- For every statement, list the exact source_ids it rests on.
- Do NOT write specific numeric lab values in your text; the values and reference
  ranges are rendered separately from the source data. Describe direction only.
- For a lab or trend statement, set asserted_status to the direction given for that
  fact in the change-set (e.g. "high", "low", "worsening"). Do not upgrade a non-critical
  value to "critical".
- Be terse and clinical. One statement per finding. No filler."""


class ClaudeNarrator:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("COPILOT_MODEL", "claude-sonnet-5")
        self.name = f"claude:{self.model}"

    def narrate(self, cs: ChangeSet, facts: dict[str, Fact]) -> tuple[SummaryDraft, dict]:
        import anthropic  # imported lazily so the stub path needs no SDK/key

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        payload = _changeset_for_llm(cs, facts)
        tool = {
            "name": "emit_summary",
            "description": "Return the structured pre-visit summary.",
            "input_schema": SummaryDraft.model_json_schema(),
        }
        resp = client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_summary"},
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        draft = SummaryDraft.model_validate(block.input)
        usage = {
            "provider": self.name,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "stop_reason": resp.stop_reason,
        }
        return draft, usage


def _changeset_for_llm(cs: ChangeSet, facts: dict[str, Fact]) -> dict:
    """Compact, source-id-keyed view. The LLM sees the allow-list explicitly so it
    cannot invent ids, and it sees each fact's permitted status."""
    return {
        "patient": cs.patient_name or cs.patient_id,
        "last_visit_date": str(cs.last_visit_date) if cs.last_visit_date else None,
        "prior_visit_date": str(cs.prior_visit_date) if cs.prior_visit_date else None,
        "facts": [
            {"source_id": f.source_id, "kind": f.kind,
             "allowed_status": f.statuses, "description": f.render}
            for f in facts.values()
        ],
    }


def get_narrator() -> Narrator:
    """Claude if a key is configured (and stubs are not forced), else the stub."""
    from .config import settings
    if settings.force_stub:
        return StubNarrator()
    if os.getenv("ANTHROPIC_API_KEY"):
        return ClaudeNarrator()
    return StubNarrator()
