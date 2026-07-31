# ARCHITECTURE.md — Clinical Co-Pilot Agent Integration Plan

How we integrate an AI agent into OpenEMR to serve the primary-care physician
defined in `USERS.md`, grounded in the findings of `AUDIT.md`. Every capability
traces to a use case; every design choice traces to an audit finding.

---

## Summary (high-level architecture and key decisions)

The Clinical Co-Pilot is a **separate Python service** that sits beside OpenEMR and
talks to it over its APIs. It is not grafted into the legacy PHP or the Angular 1.8
frontend (AUDIT architecture finding). The agent is a **LangGraph** graph driving
**Claude**, exposed through a small FastAPI surface that the OpenEMR patient view
embeds. This keeps the agent in modern, testable code and lets the same service
grow into the Week 2 multi-document and Week 3 multi-agent designs without a
rewrite.

**Authorization is inherited, not reinvented.** The agent authenticates **as the
logged-in physician** through OpenEMR's OAuth2 or SMART flow and carries that user's
JWT on every read (decision D-2, proven in the audit: CTRL-1 to CTRL-3). OpenEMR
enforces role and resource-type scopes per request. The trust boundary is the
**facility**, not the individual physician (D-5): OpenEMR does not restrict patients
by provider, and that is the intended tenancy model, so we add audit logging rather
than per-physician walls.

**Data access is shaped entirely by the performance audit.** A single FHIR read
carries about 2 seconds of fixed overhead, a serial full-context pull is about 14
seconds, and one patient's Observation history is 3 MB and 2542 resources. The agent
therefore (1) parallelizes all reads, (2) filters large result sets to a recent
window anchored on the order date, and (3) prefetches the next patient's context
during the pre-visit gap the workflow already provides (D-7). All reads stay on
FHIR (keeping enforced authorization); latency is addressed by fewer round-trips,
not by bypassing FHIR. Prefetch is a morning batch over the schedule.

**The verification layer is the heart of the design** because the audit showed the
data carries no reference ranges and no abnormal flags (DQ-2). The agent must decide
what is abnormal itself, so abnormality is computed by a **deterministic domain-rule
module keyed on LOINC**, not by the model. The model explains; the rules decide.
Verification has two jobs: **source attribution** (values are rendered
deterministically from the retrieved records with their IDs, kept out of the
model's generative path, so claims are attributed by construction, D-8) and
**domain-constraint enforcement** (reference ranges, interaction and renal-dosing
rules, rejection of malformed values like the `{entry.value}` placeholders in
DQ-6). It gates the model's output before it reaches the physician.

**Observability is wired in from the first commit** with self-hosted **Langfuse**,
chosen so traces never leave our infrastructure (a PHI concern the audit raised).
Every request carries a correlation ID propagated to all tool, FHIR, and LLM calls,
so a full trace is reconstructable from logs alone. We track tool sequence, per-step
latency, token usage and cost, and verification pass or fail rate.

**Evaluation targets failure modes, not happy paths**: missing data, malformed
input, out-of-scope access attempts, and hallucinated claims, using boolean rubrics.

**Key tradeoffs.** FHIR carries about 2 s per call, but we stay on it for enforced
authorization and cut latency by fewer round-trips, not by bypassing it. Owning
abnormality detection (DQ-2) rests on a LOINC-keyed reference-range table whose
sourcing and unit, age, and sex handling is the one real open task (D-9),
mitigated by conservative rules with the physician in the loop.

---

## 1. System topology

```
  OpenEMR patient view (browser)
        |  embeds
        v
  Co-Pilot UI panel  --HTTPS-->  Co-Pilot service (Python / FastAPI)
                                    |  LangGraph graph (Claude)
                                    |    - prefetch node
                                    |    - synthesis node (UC-1)
                                    |    - conversational node (UC-3)
                                    |    - tool nodes
                                    |    - verification gate
                                    |  carries physician's OAuth2 JWT
                                    +--FHIR (authz boundary)--> OpenEMR
                                    +--cache/prefetch--------->  (bulk reads)
                                    +--traces---------------->  Langfuse (self-hosted)
                                    +--LLM------------------->  Claude API
```

The agent is a standalone service (modern code, independently testable and
deployable) rather than a module inside the PHP monolith. It exposes `/health`
(process alive) and `/ready` (checks OpenEMR FHIR, Claude, and Langfuse
reachability) per the engineering requirements.

## 2. Data access and authorization (traces to D-2, D-5, D-7)

- **Authenticate as the user.** The agent obtains a token through OpenEMR's OAuth2
  or SMART flow and inherits the physician's scopes. It never uses client
  credentials or a system account (that would break AAA inheritance). In production
  the SMART authorization-code and EHR launch flow is preferred over password grant.
- **OpenEMR enforces access.** Role and resource-type scopes are checked per request
  (CTRL-3). We do not reimplement access control. The facility is the trust boundary
  (D-5).
- **Performance-driven read strategy (D-7):**
  - **Parallelize** the context reads (Patient, Condition, MedicationRequest,
    Observation, Encounter, AllergyIntolerance, vitals). Serial 14 s becomes about
    3 s.
  - **Filter** the fat endpoints. Observation is pulled for a recent window (for
    example the last 12 months, or last N per LOINC), anchored on
    `procedure_order.date_ordered` because the result date is empty (DQ-3). This
    protects both latency and the LLM token budget (PERF-4).
  - **Prefetch** the next patient's context when the physician opens the schedule,
    so the pre-visit summary (UC-1) is ready on demand.
- **Contracts.** Every tool input and output is a strict Pydantic schema (for
  example `LabResult{loinc, name, value, unit, date_ordered, abnormal_flag,
  source_id}`). The schema is the source of truth; raw API output never bypasses it.

## 3. Agent design (LangGraph, traces to USERS use cases)

A single conversational agent in Week 1, structured as a graph so it extends to the
Week 3 multi-agent design without a rewrite.

- **Prefetch node:** loads and caches filtered patient context in parallel (D-7).
- **Synthesis node (UC-1):** produces the "what changed since last visit" summary by
  diffing cached context against the last encounter date (dates from encounters and
  order dates, not the flattened list dates, per DQ-1).
- **Conversational node (UC-3):** answers unpredictable follow-ups, calling tools for
  anything not already cached. This multi-turn, tool-chaining behavior is the core
  justification for an agent over a dashboard.
- **Tool nodes:** typed wrappers over FHIR reads (parallelized, filtered,
  prefetched per D-7). Each tool maps to a use case; we build no tool that no use
  case needs.
- **Verification gate:** every response passes through verification before it reaches
  the physician (section 4).

Claude is the reasoning model. Tools are defined as typed contracts and invoked
through LangGraph's tool-calling.

## 4. Verification layer (traces to DQ-2, DQ-6, UC-2, UC-4)

Two responsibilities, placed as a gate between model output and the user.

- **Source attribution (provenance by orchestration, D-8).** In Week 1 the sources
  are the structured FHIR records we retrieved, each with an ID; provenance is owned
  by retrieval, not the model. Clinical values are **rendered deterministically from
  the retrieved records and kept out of the model's generative path**, so every
  stated value carries its source ID by construction and there is no "did the model
  misquote the value" problem to police. The model writes prose around
  already-attributed facts.
- **Domain-constraint enforcement (deterministic).** Because the data has no
  reference ranges or abnormal flags (DQ-2), a rules module keyed on LOINC decides
  what is abnormal. The same module applies interaction and renal-dosing rules
  (UC-4) and rejects malformed values such as `{entry.value}` (DQ-6). The model
  explains findings in prose; the rules module makes the safety determination. This
  keeps clinical decisions deterministic and testable.
- **Known limits (D-9, the one real open task).** The reference-range table must
  have a credible source and handle **units (our data is metric), age, and sex**,
  since normals differ by all three. Hardcoded guesses would be a patient-safety
  hole. We encode range plus provenance plus applicable population per LOINC, flag
  conservatively, and keep the physician in the loop. To be resolved while building
  this layer.

## 5. Observability (Langfuse, self-hosted)

- **Correlation ID** assigned per agent invocation and propagated to every tool,
  FHIR, and LLM call, so a full trace is reconstructable from logs alone.
- **Captured per encounter:** tool sequence and order, latency per step, token usage
  and cost, tool failures and retries, and verification pass or fail rate.
- **Dashboards:** request count, error rate, p50 and p95 latency, tool call counts,
  retry counts, and verification pass or fail rate.
- **PHI discipline:** self-hosted so traces stay on our infrastructure; traces log
  record IDs and metadata, not raw PHI values or patient identifiers. This becomes a
  hard, CI-checked requirement in Week 2.

## 6. Evaluation

A test suite that targets failure modes, with boolean rubrics:

- **Missing data:** patient with no labs or no encounters. Expect graceful "not on
  file", never inference.
- **Malformed input:** the `{entry.value}` placeholder (DQ-6). Expect rejection.
- **Out-of-scope access:** a request for a resource the token lacks. Expect refusal
  (mirrors CTRL-3).
- **Unsupported claim:** a hallucinated medication. Expect the verification gate to
  catch it.
- **Abnormality correctness:** a known abnormal lab. Expect the rules module to flag
  it with a cited range.
- **Boundary:** empty patient record, non-numeric result (DQ-4).

Each case documents the failure mode it guards against.

## 7. Failure modes and graceful degradation

- **Tool timeout or slow read (PERF-1):** retry with backoff, then return partial
  context clearly labeled as incomplete rather than blocking.
- **Missing record:** state that it is not on file; never infer.
- **Unexpected model output:** the verification gate rejects it; the agent asks to
  rephrase or returns a safe fallback.
- **Cold start (about 3 s):** mitigated by prefetch and keeping the stack warm.
- **Prefetch freshness:** prefetch is a morning batch over the schedule, not a
  continuous reload (too expensive). If a patient's data changes before their visit,
  refresh via hash-compare on the changed patient, else the next morning cycle.
  Real-time cache invalidation is explicitly out of scope (D-7).

## 8. Deployment (traces to D-1)

- Development is local (already running: OpenEMR plus the agent service).
- The agent is a stateless Python service, deployable beside OpenEMR. Target
  selection is open (D-1); the leading option is a single host running OpenEMR by
  compose with the agent as a companion container, evolving toward a serverless
  agent tier plus managed database later.
- `/health` and `/ready` are separate; readiness validates OpenEMR FHIR, Claude, and
  Langfuse reachability.

## 9. Key tradeoffs and risks

- **FHIR latency vs staying on FHIR.** FHIR carries about 2 s per call, but it
  enforces authorization for us. We keep it and reduce round-trips (parallelize,
  filter, prefetch, batch) rather than bypass it; bypassing would reinvent an API
  and create an authz seam for no real gain (an external agent cannot call the
  in-process service layer anyway).
- **Reference-range provenance (D-9).** Owning abnormality means our normal-range
  table must be credibly sourced and unit, age, and sex aware. The one real open
  task; conservative flagging and physician-in-the-loop until resolved.
- **Agent-owned abnormality (DQ-2).** We take on clinical-correctness responsibility.
  Mitigated by deterministic, cited, conservative rules and a physician always in the
  loop.
- **Optimistic performance baseline.** Latency numbers were measured on 12 idle
  vCPUs; a constrained production container will be slower, which raises the value of
  prefetch and filtering.

Working notes, measurements, and reproduction steps are in `DESIGN_NOTES.md`.
