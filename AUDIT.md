# AUDIT.md — OpenEMR Pre-Build Audit

Audit of the forked OpenEMR base (`Gauntlet-HQ/openemr-base-clean`, OpenEMR 8.2.0)
performed before designing the Clinical Co-Pilot agent. Findings are grounded in
the running instance (20 Synthea patients) and the source code. Positive controls
and risks are both recorded: an audit is a map of what constrains the build, not
only a list of defects.

---

## Summary (key findings)

**The single most important finding is empirical, not theoretical: the platform is
slow enough to reshape the entire agent design.** A single FHIR read carries about
2 seconds of fixed overhead (PHP boot, OAuth validation, FHIR serialization),
independent of payload. Fetching one patient's full clinical context serially takes
about 14 seconds, which is unusable for a physician who has 90 seconds between
rooms. Running the same reads in parallel collapses this to about 3 seconds, and
because the pre-visit summary happens before the physician enters the room, the
workflow itself provides a prefetch window. So the agent must parallelize reads,
prefetch during the schedule view, and filter large result sets. This is not a
tuning preference; the measurements make it mandatory (PERF-1 to PERF-4).

**The second finding reshapes the trust model.** OpenEMR enforces access at the
role and resource-type level but not at the patient level: any authorized physician
can read any patient's full chart. This was proven live by creating two physicians
and confirming one could read the other's patient. Rather than a defect, this is
the intended tenancy model. OpenEMR isolates tenants per deployment (multisite
gives each clinic its own database), and physician turnover, cross-cover, and
on-call all require broad within-facility access. The decision (D-5) is therefore
that the trust boundary is the deployment or facility, not the physician, and the
compliance control is audit logging rather than per-physician walls, matching how
Epic and Cerner operate.

**The third finding drives the verification layer.** Lab data carries a LOINC code
on 100 percent of results, but zero results carry a reference range or an abnormal
flag. The agent therefore cannot ask the EHR whether a value is abnormal; it must
apply its own LOINC-keyed reference ranges, and that logic must be grounded and
verifiable. Deciding what to worry about is, by necessity, the agent's job.

**Positive controls we can rely on.** The OAuth2 layer is well designed: clinician
scopes require a confidential client (CTRL-1), confidential clients register
disabled pending admin approval (CTRL-2), and the FHIR API enforces scopes per
request (an out-of-scope resource returns 401, CTRL-3). This means the agent can
authenticate as the physician and inherit role and resource-type authorization for
free (decision D-2), verified end to end.

**Risks to close before any public deployment.** A GitHub token is committed in
plaintext across several compose files (SEC-1); default credentials pervade the dev
stack (SEC-1b); dynamic client registration is unauthenticated (SEC-3); and
patient-scoped public clients register already enabled (SEC-4). None block local
work, but all must be remediated before exposure.

**Data readiness.** Coverage and coding are strong (all patients populated, 100
percent problem and medication coding). Three constraints shape the build: lab
result dates are empty so trends must anchor on the order date (DQ-3); about 26
percent of results are non-numeric (DQ-4); and some values imported as malformed
placeholders that the verification layer must reject (DQ-6). Blood pressure and
weight did not import and were remediated with a documented synthetic backfill
(DQ-5).

**Net effect on the plan:** the agent will authenticate as the user, parallelize
and prefetch filtered reads, own abnormality detection with grounded reference
ranges, scope trust to the facility, and log every PHI access.

---

## 1. Security audit

### Positive controls (inheritable by the agent)
- **CTRL-1: clinician scopes require a confidential client.** Public clients are
  refused `user/*` and `system/*` scopes ("system and user scopes are only allowed
  for confidential clients"). The gate is the `application_type: "private"` flag at
  registration (`src/RestControllers/AuthorizationController.php:312`), which issues
  a secret and sets `client_role: user`.
- **CTRL-2: confidential clients register disabled.** New clients have
  `oauth_clients.is_enabled = 0` and require admin approval before a token can be
  issued (verified: token request failed with `invalid_client` until enabled).
- **CTRL-3: per-request scope enforcement.** With a token granted Patient,
  Condition, and MedicationRequest but not Observation, the first three returned
  200 and Observation returned 401. Enforcement is at the API layer, done by
  OpenEMR. This is the basis of decision D-2 (the agent authenticates as the user
  and inherits scopes).

### Risks
- **SEC-1 (secrets in repo):** a GitHub personal access token is committed in
  plaintext (`GITHUB_COMPOSER_TOKEN`) plus two reversible "encoded" variants, across
  four compose files and `docker/flex/openemr.sh`. Obfuscation, not encryption.
- **SEC-1b (default credentials):** `admin/pass`, MySQL `root/root` and
  `openemr/openemr`, CouchDB and VNC defaults throughout the dev stack. Must never
  reach a public deployment.
- **SEC-3 (open registration):** `POST /oauth2/default/registration` is
  unauthenticated. Standards-compliant for SMART, but on a public host it lets
  anyone mint client records.
- **SEC-4 (asymmetric enablement):** patient-scoped public clients register already
  enabled (`is_enabled = 1`), unlike confidential clients (CTRL-2). Risk bounded by
  still needing a patient login, but the default is worth hardening.
- **SEC-5 (no row-level access):** any authorized physician can read any patient.
  Reclassified as intended tenancy, see Compliance and decision D-5.

## 2. Performance audit

Measured against a data-rich patient. Compute baseline: the app container sees 12
vCPU with no memory cap, nearly idle. Numbers are therefore optimistic; a
constrained production container (1 to 2 vCPU) would be slower.

| Endpoint | KB | resources | p50 ms |
|---|---|---|---|
| Patient/{id} | 2.2 | 1 | 2947 (cold start) |
| Condition | 223.7 | 136 | 1816 |
| MedicationRequest | 15.2 | 18 | 2381 |
| Observation | 3018 | 2542 | 3483 |
| Encounter | 94.1 | 115 | 1884 |
| AllergyIntolerance | 2.3 | 2 | 1627 |
| Serial full context | | | 14139 |
| Parallel (6 concurrent) | | | 2989 |

- **PERF-1:** about 1.6 to 2.4 seconds of fixed per-request overhead, independent
  of payload (a 223 KB response was faster than a 2 KB one). First call about 3
  seconds (cold start).
- **PERF-2:** serial full context about 14 seconds, unacceptable before any LLM
  call.
- **PERF-3:** parallelizing yields about 3 seconds (4.7x), bounded by the slowest
  call.
- **PERF-4:** Observation is 3 MB and 2542 resources for one patient: the latency
  long-pole and an LLM context and token blowout. Must be filtered server-side to a
  recent window.

**Design implications (see D-7):** parallelize all reads; filter fat endpoints to a
recent window (anchored on order date per DQ-3); prefetch the next patient's context
during the pre-visit gap; consider a hybrid data path (FHIR at the authz boundary,
direct or cached service reads for bulk and prefetch); keep the stack warm to avoid
cold starts.

## 3. Architecture audit

- **Two-world codebase.** `library/` (about 617 files) is 2005-era procedural PHP
  with global state; `src/` (about 2115 files) is modern PSR-4 (Laminas plus
  Symfony, Doctrine, typed value objects, PHPStan level 10). New work belongs in
  `src/`.
- **Data spine.** Patient has many Encounters; clinical data (problems, meds, labs,
  vitals) attaches to encounters. Scheduling and billing wrap around this core.
  Notable: form data such as vitals requires a paired row in the `forms` registry
  to appear in an encounter and via FHIR.
- **Integration surface.** Two clean options. (a) The service layer
  (`src/Services/*Service.php`, about 90 domain services returning records with
  IDs) is a typed tool surface. (b) FHIR R4 plus US Core plus SMART on FHIR v2.2.0,
  secured by OAuth2 with granular scopes, is the standardized, authorization-aware
  surface. The agent will favor FHIR at the trust boundary and may use the service
  layer for bulk or prefetch (D-7).
- **Access control model.** phpGACL with 6 role groups (Administrators, Physicians,
  Clinicians, Front Office, Accounting, Emergency Login) and 65 discrete
  permissions. A valid API user also needs membership in the legacy `groups`
  authorization table.
- **Deployment reality.** The base repo has no deployment pipeline
  (`cloudbuild.yaml` only builds assets). The dev stack is a seven-container
  compose. Public deployment is infrastructure we design (D-1).
- **Frontend debt.** Angular 1.8 (end of life) plus jQuery plus dual template
  engines (Twig and Smarty). The agent UI should be a cleanly separated modern
  surface, not grafted into the legacy frontend (D-3).

## 4. Data-quality audit

Profiled the exact data the agent reads. 20 patients.

- **Strengths:** full coverage (all patients have encounters, problems, vitals,
  labs; 19 of 20 have meds); 100 percent of problems and meds coded; 100 percent of
  lab results carry a LOINC and a name; lab trending is feasible once anchored
  correctly.
- **DQ-2 (critical, drives verification):** 0 of 6489 lab results carry a reference
  range or an abnormal flag. The agent must apply its own LOINC-keyed reference
  ranges and abnormality logic, grounded and verifiable.
- **DQ-3 (critical, resolved):** `procedure_result.date` is empty for all rows;
  `procedure_order.date_ordered` is fully populated. Anchor lab timelines on the
  order date.
- **DQ-4:** about 26 percent of results are non-numeric (qualitative). Handle both.
- **DQ-5 (gap, remediated):** blood pressure and weight did not import (Synthea
  encodes BP as a component observation the CDA parser does not map). Remediated
  with a documented, condition-correlated synthetic backfill across 784 encounters,
  each linked to its encounter via the `forms` registry.
- **DQ-6 (defect):** some qualitative results imported the literal placeholder
  `{entry.value}`. The verification layer and tool schemas must reject malformed
  values. Concrete eval boundary case.
- **DQ-1:** the CDA import flattened medication and problem `date` fields to the
  import time, so "what changed" must key off sources that kept real dates
  (encounters, order dates), not the list dates.

## 5. Compliance and regulatory audit

- **Audit logging (HIPAA 164.312(b)).** OpenEMR ships an audit subsystem
  (`EventAuditLogger`, the `log` table, viewable under Administration then Logs),
  which already records queries, updates, API calls, and auth events. The agent
  extends this: every PHI access must be logged with a correlation ID that ties the
  full request chain (tool calls, FHIR reads, LLM calls) to one identifiable user.
  Given SEC-5, audit logging is the primary minimum-necessary control.
- **Minimum necessary (HIPAA Privacy Rule).** The agent must request only the
  scopes and data a use case needs, and must filter large result sets (PERF-4)
  rather than pull whole records. Filtering is both a performance and a compliance
  control.
- **BAA and LLM providers.** Per project guidance we assume a signed BAA with the
  LLM provider covering no training on data. Compliance obligations that remain:
  treat prompts, extracted fields, and outputs as PHI; do not send PHI to
  observability or logging tools that are not covered; and scrub PHI from traces and
  metrics (this becomes a hard requirement in Week 2).
- **Data retention.** HIPAA and state law impose multi-year retention on clinical
  and audit records. Retention is a deployment configuration concern; the agent
  must not delete source records and its audit logs must be retained with them.
- **Breach notification.** The Breach Notification Rule applies if PHI is exposed.
  The security risks above (SEC-1, SEC-3, SEC-4, default credentials) are precisely
  the exposure vectors to close before any public deployment.
- **Break-glass (SEC-2).** The Emergency Login role is an access override. No users
  hold it by default. The agent must never operate under break-glass, and any
  break-glass session must be flagged in the agent's audit log.

---

## How the audit changed the AI integration plan

1. The agent authenticates as the user and inherits role and resource-type
   authorization (CTRL-1 to CTRL-3, D-2), and does not implement per-physician
   patient walls (SEC-5, D-5).
2. The agent parallelizes and prefetches filtered reads to meet the latency budget
   (PERF-1 to PERF-4, D-7).
3. The agent owns abnormality detection with its own grounded reference ranges
   (DQ-2), and its verification layer rejects malformed inputs (DQ-6).
4. Trust is scoped to the facility and enforced through audit logging and minimum
   necessary, not per-physician restriction (D-5, Compliance).

Detailed working notes, measurements, and reproduction steps are in
`DESIGN_NOTES.md`.
