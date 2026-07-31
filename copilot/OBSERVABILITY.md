# Observability (Langfuse)

The agent is instrumented for full-trace observability. Tracing is **optional and
guarded**: with no Langfuse keys set it is a no-op with zero overhead, and any SDK
error degrades to a no-op rather than breaking the clinical path (`app/trace.py`).

## What gets traced

Semantic span tree, so a trace reads clinically:

- **UC-1** (`uc1_previsit_synthesis`, type `agent`)
  - `prepare` (`span`) - fact/abnormal/new-med counts, per-resource `fetch_ms`
  - `narrate` (`generation`) - model name, **input/output token usage**
  - `verify` (`guardrail`) - claims passed vs dropped
  - trace score **`verification_pass_rate`** (0..1)
- **UC-3** (`uc3_chart_qa`, type `agent`)
  - one `tool` span per grounded tool call, with its input and returned rows
  - token usage on the root
  - trace score **`grounded`** (BOOLEAN) with the violation detail as comment

These cover the Week-1 dashboard asks: request/latency (span durations),
token cost (usage), tool calls (tool spans), and verification pass/fail (scores).

## Turn it on

Set three variables in `copilot/.env` (see `.env.example`) and re-run:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000        # or https://cloud.langfuse.com
```

Then `python run_agent.py` / `python run_chat.py` emit traces automatically.

## Choosing a sink

**Path A - Langfuse Cloud free tier (light, fine for dev).**
Create a project at cloud.langfuse.com, copy the two keys, set `LANGFUSE_HOST` to
the cloud URL. Acceptable here because our dev data is **Synthea (synthetic), not
real PHI**. Not for production.

**Path B - self-hosted (matches the ARCHITECTURE decision for PHI control).**
Langfuse v3 self-host is ~6 containers (web, worker, postgres, clickhouse, redis,
minio). Use the official compose:

```
git clone https://github.com/langfuse/langfuse
cd langfuse && docker compose up -d          # serves http://localhost:3000
```

Then set `LANGFUSE_HOST=http://localhost:3000` and the keys from the UI.
Note the memory cost: running this alongside the OpenEMR stack on a 16 GB machine
is heavy. For day-to-day dev on this box, Path A is the pragmatic choice; stand up
Path B when demonstrating the production-shaped, PHI-safe deployment.
