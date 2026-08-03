# Clinical Co-Pilot: Decisions and Progress

A running summary of what was decided and what is built. The detailed reasoning
lives in `DESIGN_NOTES.md`; the graded deliverables are `AUDIT.md`, `USERS.md`,
and `ARCHITECTURE.md`. This file is the quick map.

Last updated: 2026-08-02. Branch `feat/clinical-copilot-foundation`, pushed to the
personal fork `sainathyai/openemr-base-clean` (remote `fork`; `origin` remains the
Gauntlet-HQ base, read-only). 58 tests green, plus a 21-case golden eval corpus at
21/21. Now in the deployment/UI phase: a FastAPI serving layer + single-page UI is
live locally, driving all three use cases in the browser on real FHIR data. Deploy
target chosen: a single AWS EC2 t3.micro (free tier). UC-2 (order safety check) is
built: a deterministic, sourced engine over a curated knowledge table, wired into a
third UI surface.

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
| D-1 | Local Docker for dev; public deploy on a single AWS EC2 t3.micro (free tier). One box runs the trimmed OpenEMR compose + the copilot serving layer; 2-4 GB swap, DB restored from dump not re-imported, Caddy for real TLS. RDS-micro for the DB is the fallback if 1 GB is too tight | Target chosen; not yet deployed |
| D-2 | Agent authenticates AS the physician via OAuth2; inherits OpenEMR AAA (role + resource-type scopes), never reimplements authz | Proven end to end |
| D-3 | Agent UI surface: a separate modern surface, not grafted into OpenEMR's Angular 1.8. Built as a FastAPI serving layer (`copilot/server/`) that serves the API and a single-page UI from ONE uvicorn process (no Node runtime, so it fits the t3.micro budget). Standalone page + patient picker for now; iframe-into-the-patient-screen is a later refinement | Building |
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

4b. **Blood pressure component parser** (DQ-5). BP Observations carry no top-level
   value: systolic (LOINC 8480-6) and diastolic (8462-4) live in `component[]`, so
   BP surfaced as None. `_obs_value` now renders the panel as a single "systolic/
   diastolic" vital (e.g. `154/88 mmHg`), normalizing `mm[Hg]`, with a single-
   component fallback. Both numbers land in the grounding ledger. Verified live on
   the demo patient (154/88, 155/89, ... the hypertensive backfill) and in 10 unit
   tests. Test-only observability was also silenced (`conftest.py`) so the suite
   never flushes stub traces to the live Langfuse project.

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

6. **Serving layer + UI shell (D-3)** (`copilot/server/`). A thin FastAPI app that
   wraps the existing agents and serves a single-page UI from the same process (one
   uvicorn worker, ~150-250 MB, so it fits the t3.micro target). No new clinical
   logic: endpoints call `agent.run` (UC-1), `get_chat` (UC-3), and a patient roster
   read, then serialise the typed results. The UI is a patient picker plus three
   surfaces: the pre-visit briefing (verified statement cards with deterministic
   evidence, source-id chips, a trust badge, and a collapsible list of the claims the
   gate dropped, so the verification is visible), the chart-Q&A chat (per-answer
   grounded/ungrounded badge + citations), and the order-check form (UC-2, wired to a
   stub endpoint pending its logic). Verified live end to end on the 20-patient set
   with `COPILOT_FORCE_STUB=1`: briefing 12/12 on Pfeffer, 14/14 on Aracely, chat
   grounded across change-set/allergy/lab questions, all with zero Claude spend. A new
   `COPILOT_FORCE_STUB` switch forces the deterministic stubs even when a key is set,
   for zero-spend demos/tests and as a deployment kill switch.

7. **UC-2 order safety check** (`app/order_safety.py` + `app/data/order_knowledge.json`).
   The third interactive surface, and deterministic like the rest: a physician types a
   proposed order, and a curated drug table (class, renal handling, allergen class,
   indications, contraindications, with provenance; the D-9 pattern) drives four sourced
   checks: allergy (direct + class cross-reactivity), renal (the drug's renal profile
   against the patient's own creatinine/eGFR via `classify_all`), duplicate therapy +
   additive bleeding risk (against active meds), and indication/contraindication (against
   the active problem list). The LLM is not in the path; every finding cites its FHIR
   source_id, findings are severity-ranked (danger/caution/info/ok) and de-duplicated,
   and an unrecognized drug returns "not recognized" with NO clearance (silence is never
   safety). Problem matching uses whole-word matching and skips historical/contextual
   entries, so "Past pregnancy history of miscarriage" no longer misfires an ACE-inhibitor
   contraindication (regression-tested). Verified live on Pfeffer (CKD/HTN, on clopidogrel):
   ibuprofen -> danger (renal + CKD contraindication + bleeding), metformin -> danger,
   lisinopril -> caution (renal monitor, appropriate for HTN), amoxicillin -> caution. 13
   engine tests. Endpoint `POST /api/patients/{uuid}/order-check`; UI renders severity
   cards with source chips and a "what was checked" footnote.

## Validation state

- 58 tests pass (`copilot/tests/`), no live OpenEMR needed: the UC-2 order-safety
  engine (allergy/renal/duplicate/bleeding/indication/contraindication, the
  unrecognized-drug fail-safe, and the historical-problem regression); D-9 boundaries
  (bands, sex-specificity, unit mismatch, pediatric, inverted HDL, target-based
  cholesterol), every gate rejection (unknown source, wrong direction, invented
  number, fabricated critical, category mismatch, full-hallucination block, UC-3
  grounding, DQ-6 sanitation), the BP component parser (DQ-5), and a guard that
  tracing is off during tests.
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

## Milestone: Week-1 engineering foundation complete

Everything that makes the product *trustworthy* is built and proven, end to end,
against live OpenEMR and in a hermetic test suite. In one line: the co-pilot reads a
patient's real record over FHIR, computes the clinically important facts
deterministically, lets Claude narrate only those facts, and verifies every claim
before it reaches the physician, with the whole path traced for cost and quality.

What is done:
- **Data access** (D-7): auth as the physician, parallel FHIR pull, filtered and
  typed, `source_id` on every fact. ~4s versus ~12s serial.
- **UC-1 pre-visit synthesis** with a verification gate that fails CLOSED. The LLM is
  out of the truth path (D-8); abnormality is owned by a sourced reference table
  (D-9). Proven: adversarial narrator has fabrications dropped.
- **UC-3 conversational Q&A** with a grounding gate that flags any citation or number
  no tool surfaced.
- **Observability** (Langfuse): semantic spans, token cost, and quality scores.
- **Reliability of the evidence:** BP parsed from `component[]` (DQ-5), placeholders
  scrubbed (DQ-6), a 21-case golden eval corpus (both gates, with a negative
  control), and 45 hermetic tests. Tests never touch the live Langfuse or spend
  credits.

Decisions locked for now: D-2, D-4, D-5, D-6, D-7, D-8, D-9 (see the table). Real
Claude has been validated once on Haiku; the rest runs offline on stubs by design.

## Deployment and UI phase (in progress)

The headless core is now reachable and visible: the FastAPI serving layer + UI (build
items 6-7) drive all three use cases in the browser on live FHIR data. The two decisions
that gated this phase are made and built: UC-2 is the order safety check (done), and the
deploy target is a single AWS EC2 t3.micro (D-1). Remaining work, in order:

1. **Server-layer tests:** the UC-2 engine is tested (13 cases); still to add are
   hermetic tests for the FastAPI serialisers/endpoints (`_briefing_payload`, the
   order-check endpoint shape) using a stubbed context, alongside the existing 58.
2. **Public deploy (D-1):** provision the t3.micro (swap, trimmed compose, DB restore,
   Caddy/TLS, OAuth redirect URIs), then point it at the same compose the dev loop
   uses. Bring `COPILOT_FORCE_STUB` off only when a Claude budget is confirmed.
3. **Polish for the demo:** optional LLM narration layer over UC-2 findings (kept out
   of the truth path, same as UC-1), and iframe-embedding the panel into OpenEMR's
   patient screen (currently a standalone page + picker).

Deferred backlog (logged, not blocking): run the golden corpus once through real
Haiku and log the two rates to Langfuse; the eval gate gap where a wrong-analyte
`source_id` with a consistent direction is not yet caught; a data-quality thread
where some non-BP vitals (respiratory rate, temperature, O2 sat) surface None on
recent draws, likely DQ-6 placeholders on those entries; and multi-turn chat memory
(the chat endpoint is currently single-turn/stateless, which is fine for the demo).
