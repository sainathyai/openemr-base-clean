# Clinical Co-Pilot: Decisions and Progress

A running summary of what was decided and what is built. The detailed reasoning
lives in `DESIGN_NOTES.md`; the graded deliverables are `AUDIT.md`, `USERS.md`,
and `ARCHITECTURE.md`. This file is the quick map.

Last updated: 2026-08-01. Branch `feat/clinical-copilot-foundation`, 7 feature
commits, pushed to the personal fork `sainathyai/openemr-base-clean` (remote
`fork`; `origin` remains the Gauntlet-HQ base). 34 tests green, plus a 21-case
golden eval corpus at 21/21.

## What this is

Week 1 of the AgentForge build: an AI clinical co-pilot embedded on a fork of
OpenEMR 8.2 (`Gauntlet-HQ/openemr-base-clean`). Target user is a primary-care
physician doing a fast pre-visit chart review and in-visit questions. The product
principle throughout: the LLM narrates facts that are already computed and already
sourced, and a verification layer checks every claim before it reaches the
physician. We build to an enterprise-grade bar, not to the letter of the brief.

## Environment and stack

- **Run locally** for dev (Docker), not cloud, because of a 16 GB RAM limit and
  because Docker rebuild loops are fast. Public deploy is a later stage (D-1).
- **OpenEMR 8.2** (PHP 8.2 monolith, MariaDB, FHIR R4 + SMART on FHIR + OAuth2).
  Running via a trimmed `docker/development-easy` compose (mysql + openemr +
  phpmyadmin). App at https://localhost:9300.
- **Agent service:** Python + LangGraph + Claude + Langfuse, in `copilot/`,
  running beside OpenEMR and talking to it over FHIR. No changes to OpenEMR core.
- **Data:** 20 Synthea synthetic patients loaded through OpenEMR's built-in CCDA
  importer (877 encounters, 758 problems, 122 meds, 6489 lab results).

## Key decisions

| ID | Decision | Status |
|----|----------|--------|
| D-1 | Local Docker for dev; public deploy is a later stage | Decided; deploy open |
| D-2 | Agent authenticates AS the physician via OAuth2; inherits OpenEMR AAA (role + resource-type scopes), never reimplements authz | Proven end to end |
| D-3 | Agent UI surface: a separate modern surface embedded in the patient view, not grafted into OpenEMR's Angular 1.8 | Open |
| D-4 | Synthetic data via OpenEMR's built-in Synthea CCDA importer, not hand-rolled inserts | Resolved |
| D-5 | Trust boundary is the hospital/clinic DEPLOYMENT, not the physician. OpenEMR has no row-level patient walls and that is intended (multisite = per-DB tenancy; staff turnover and cross-cover make per-physician walls wrong). Agent does role + resource-type (+ facility) scoping and audit logging | Decided |
| D-6 | Agent is a READER of the consolidated record, not an integrator. It does not pull live from pharmacies or external specialists; interoperability is OpenEMR's job | Decided |
| D-7 | FHIR read strategy: authenticate as user, read all resources in PARALLEL, FILTER fat endpoints (Observation) to a recent window, prefetch during the pre-visit gap. No service-layer bypass | Built and proven |
| D-8 | Source attribution is an orchestration property, not an LLM task. Values and citations are rendered deterministically from source data and kept out of the LLM path | Built into the gate |
| D-9 | Reference-range table (LOINC-keyed, unit + age + sex aware, with provenance) owns lab abnormality, since the data carries none | Resolved |

## Audit findings that shaped the build

- **Security:** OAuth2 user-scoped access works (RS256 JWT, per-request scope
  enforcement, out-of-scope returns 401). Confidential client needs
  `application_type: private`. No row-level patient access (reframed as intended
  per-deployment tenancy, D-5). Findings SEC-1..5, CTRL-1..3 in DESIGN_NOTES.
- **Performance:** roughly 2s fixed overhead per FHIR call. Serial full context is
  14s (unacceptable); parallel is about 3s. Observation is 3 MB / 2500+ resources
  for one patient and must be filtered. Drove D-7.
- **Data quality:** labs carry 0% reference ranges or abnormal flags (DQ-2, drove
  D-9). Result dates empty, anchor on order date (DQ-3). About 26% non-numeric
  values (DQ-4). BP/weight did not import from Synthea (DQ-5, backfilled). Malformed
  `{entry.value}` placeholders present (DQ-6, now sanitized at the FHIR boundary).

## Build progress

All in `copilot/`. Python + httpx + pydantic + langgraph + anthropic + langfuse.

1. **Data-access foundation** (`aec632c`). Async FHIR client, auth as user, parallel
   pull of all 7 resource types, lab filtering, typed `PatientContext` with a
   `source_id` on every fact. Pfeffer full context in about 4s versus 12s serial.

2. **UC-1 pre-visit synthesis + verification gate** (`cbd0f7c`). LangGraph graph
   `prepare -> narrate -> verify -> render`. The LLM is kept out of the truth path:
   - `reference_ranges.py` + `data/reference_ranges.json`: the D-9 table, grounded
     in the actual LOINC codes and units in the loaded data. Unit-checked,
     sex-aware, age-fail-safe (under 18 refuses adult ranges), direction-aware,
     provenance per analyte.
   - `changes.py`: finds the prior visit, computes new problems/meds, lab trends,
     and current abnormals/criticals.
   - `summary.py`: the gate. The LLM emits structured claims (category, text,
     source_ids, asserted_status); the gate enforces source attribution, domain
     constraint (asserted direction must match the table verdict), and
     no-unsourced-numbers. It fails CLOSED and drops unverifiable claims.
   - `llm.py`: swappable narrator. `ClaudeNarrator` (forced structured tool-use) or
     a faithful `StubNarrator` that needs no API key, so the graph and tests run
     offline. Proven: faithful run verified 12/12; an adversarial narrator that
     invents a source, flips a direction, and fabricates a value had 3 of 4 dropped.

3. **UC-3 conversational chart Q&A** (`fe3be68`). Multi-turn, built as tool-calling
   over the in-memory context (not a raw dump). `tools.py` has six grounded tools
   feeding a per-turn ledger of source_ids, numbers, and counts. `chat.py` runs the
   Claude tool-use loop and must answer through a structured `respond` tool; the
   grounding gate flags any citation or number a tool did not surface. UC-1 fails
   closed (autonomous); UC-3 flags but shows the answer (interactive). DQ-6 fixed
   globally at the FHIR boundary.

4. **Langfuse observability** (`61ad437`, `0a4b53c`, `85e70e2`). Optional and
   guarded (no keys means a zero-overhead no-op; any SDK error degrades to no-op).
   Semantic span tree: UC-1 as agent -> {prepare span, narrate generation with token
   usage, verify guardrail} plus a `verification_pass_rate` score; UC-3 with tool
   spans and a `grounded` score. `OBSERVABILITY.md` documents the two sink options.

5. **Golden eval corpus** (`copilot/eval/`). A scored, stub-only corpus that turns
   "the gate worked once" into "the gates hold across the corpus." Two suites: a
   13-case fail-closed gate suite (faithful claims survive; unknown source, flipped
   direction, fabricated decimal/integer, fabricated critical, category mismatch,
   mixed, and full-hallucination drafts are dropped with the expected violation rule
   and never rendered) driving the real `build_facts -> verify_draft` path; and an
   8-case UC-3 grounding suite (faithful lab/allergy/med/problem/change-set questions
   come back grounded and cited, an absent lab is answered honestly, and fabricated
   citations/numbers are flagged) driving the offline `StubChat` and `check_grounding`.
   `python -m eval.run` prints a scorecard with two headline rates that mirror the
   Langfuse scores; `tests/test_eval.py` runs it in CI. A negative-control check
   confirms the harness fails when an expectation is wrong, so green is meaningful.

## Validation state

- 34 tests pass (`copilot/tests/`), no live OpenEMR needed: D-9 boundaries
  (bands, sex-specificity, unit mismatch, pediatric, inverted HDL, target-based
  cholesterol) and every gate rejection (unknown source, wrong direction, invented
  number, fabricated critical, category mismatch, full-hallucination block, UC-3
  grounding, DQ-6 sanitation).
- Golden eval corpus at 21/21 (`copilot/eval/`, `python -m eval.run`):
  verification_pass_rate 1.00, grounded_rate 1.00 on the deterministic path.
  Guarded by `tests/test_eval.py` and a negative-control check.
- Langfuse Cloud (US) authenticated and flushing traces.
- Real Claude validated once on Haiku: UC-1 verified 11/11, tokens 2187 in / 808
  out (about $0.006), trace with token usage and score in the dashboard.

## Conventions

- Credits are conserved: cheap single calls, Haiku for tests
  (`COPILOT_MODEL=claude-haiku-4-5-20251001`), no multi-call demo runs, no PDF or
  image extraction. Any Claude spend is called out first.
- Configuration in `copilot/.env` (gitignored); template in `.env.example`. Without
  an Anthropic key the deterministic stubs run; without Langfuse keys tracing is off.

## Next

1. DONE. Golden eval corpus at 21/21 (see Build progress 5). Next extension when
   there is budget: run the same corpus once through real Claude (Haiku) and log the
   two rates to Langfuse, to confirm the live model also clears the gates.
2. BP `component[]` parser so blood pressure surfaces (deterministic, no Claude cost).
3. Decide UC-2, then the agent UI surface (D-3).
4. Public deploy (D-1).
