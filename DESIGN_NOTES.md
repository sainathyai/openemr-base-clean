# Clinical Co-Pilot — Design Notes & Decision Log

> Running log of observations, design choices, and open decisions as we get
> familiar with OpenEMR and plan the agent. Lightweight ADR style. Newest
> context at top of each section. Not a graded deliverable — this is our
> engineering memory so decisions are defensible later.

---

## Observations captured so far (2026-07-29)

### O-1. Two-world codebase: `src/` (modern) vs `library/` (legacy)
- `library/` (617 files): 2005-era procedural PHP — global state, `$GLOBALS` /
  `$_SESSION` as service locator, loose comparisons. The repo's own CLAUDE.md
  labels these antipatterns and says never to copy them.
- `src/` (2115 files): modern PSR-4, Laminas MVC + Symfony, Doctrine, DI, typed
  immutable value objects, PHPStan level 10.
- **Implication:** build the agent in the `src/` world. Ride the modern service
  layer; never justify sloppy code by pointing at legacy.

### O-2. The `src/Services/*Service.php` layer is the agent's tool surface
- ~90 domain services (PatientService, MedicationPatientIssueService,
  ObservationLabService, EncounterService, …) give typed, structured access to
  the same data the UI shows, returning records with **IDs**.
- **Implication:** the agent should call services (or the FHIR API on top of
  them), not raw SQL or scraped UI. Record IDs are what make the required
  "every claim traces to a source" property achievable cheaply.

### O-3. SMART on FHIR v2.2.0 + OAuth2 granular scopes already exists
- FHIR R4 + US Core 8.0, OAuth2 / OpenID Connect, per-resource scopes
  (`patient/Observation.rs`). Pre-enabled in the dev compose.
- **Implication:** the "who is allowed to query patient data" hard problem likely
  has an existing, standards-based answer we enforce against rather than invent.
- **Open question (see D-2):** does the agent authenticate *as the physician*,
  inheriting their scopes? Leaning yes — it makes the authz boundary automatic.

### O-4. Shipped sample data is insufficient for a clinical agent — CONFIRMED
- `sql/example_patient_data.sql` = ~14 patients, **demographics only** (28 lines).
  No encounters, labs, meds, or vitals in the seed.
- **CONFIRMED empirically (2026-07-30):** after a clean install the DB has 283
  tables (full schema) but **`patient_data` = 0 rows.** The `flex` easy-dev image
  installs the schema and the `admin` user but loads **no demo patients at all.**
- **Implication / action:** provisioning realistic synthetic clinical data is a
  hard **build prerequisite**, not polish. An empty chart gives the agent nothing
  to reason over. Also a data-quality audit finding. See D-4.

### O-7. OAuth2 keypair not generated on first boot
- `/meta/health/readyz` reports `oauth_keys:false` on a fresh install (the app is
  otherwise healthy: version 8.2.0, `admin` active, login serves, root 302→login).
- **Implication:** the SMART-on-FHIR / OAuth2 path (our leading agent integration,
  O-3) requires generating the OAuth2 keys before the FHIR API is usable. We must
  do this deliberately — and audit it while we're there. Note `readyz` also shows
  `installed:false` despite a working install; that probe reads a specific marker
  (likely `sqlconf.php $config` / oauth keys), not the schema — treat the app as
  installed (283 tables, admin user, login works).

### O-5. Angular 1.8 front end is end-of-life
- Front end is Angular 1.8 + jQuery 3.7 + Bootstrap 4.6. Angular 1.x is EOL.
- **Implication:** we likely do NOT extend the legacy Angular UI. The agent UI
  should be a cleanly separated modern surface (decision pending, see D-3).

### O-6. Deployment is unsolved in the base repo
- `cloudbuild.yaml` only runs `npm install && npm run build` — it does not deploy.
- The dev stack is a 7-container docker-compose (openemr, mariadb, phpmyadmin,
  selenium, couchdb, openldap, mailpit).
- **Implication:** the public deployment (a hard gate) is infrastructure we design
  ourselves. See D-1.

---

## Security findings — secrets & config (documented)

- **SEC-1 (documented):** a **GitHub PAT is committed in plaintext** as
  `GITHUB_COMPOSER_TOKEN` (raise composer/GitHub rate limits), plus two
  "encoded" variants (`_ENCODED` base64, `_ENCODED_ALTERNATE` ASCII-codes) —
  which is reversible **obfuscation, not encryption** (security theater).
  Present in **4 compose files** (`development-easy`, `-light`, `-redis`,
  `-insane`) **and `docker/flex/openemr.sh`**. Upstream OpenEMR project's shared
  burner token, low blast radius, but a textbook secret-in-repo finding.
  **For our deployment:** never carry this pattern — secrets via env/secret
  manager, never committed; scrub before any public push.
- **SEC-1b:** default creds throughout the dev stack (`admin/pass`, MySQL
  `root/root` + `openemr/openemr`, CouchDB `password`, VNC `openemr123`). Fine
  for local; **must never reach a public deployment** — rotate/parameterize all.
- **SEC-2 (documented):** `breakglass` / Emergency Login is an access-override
  role. **Verified: no users are assigned to it by default** (good). It is a
  global emergency mechanism (`library/globals.inc.php`, `usergroup_admin.php`).
  **Agent posture:** the agent must never operate under break-glass, and any
  break-glass session should be flagged in agent audit logs. HIPAA-sensitive:
  every use must be logged and reviewed.

_(Security/controls audit is now substantially complete. Perf / data-quality /
architecture / compliance passes still to do — lower stakes, can follow user
selection. See "Controls audit — OAuth2/FHIR" above for the authz findings.)_

---

## Controls audit — OAuth2 / FHIR (hands-on, 2026-07-30)

Live investigation of the access-control system that the agent will inherit.
Registration/token/FHIR round-trip run against the local instance. Findings
below feed AUDIT.md (security + compliance sections). Positive controls (CTRL)
and risks (SEC) both recorded — an audit is not only a bug list.

### Built-in role model (phpGACL)
- 6 role groups: **Administrators** (`admin`), **Physicians** (`doc`),
  **Clinicians** (`clin`), **Front Office** (`front`), **Accounting** (`back`),
  **Emergency Login** (`breakglass`). 65 discrete permissions (ACOs).
- These are the user *types*; the agent inherits whichever the logged-in user has.
- **SEC-2 (parked, HIPAA):** the `breakglass` / Emergency Login role is an
  access-override ("break glass") — every use must be audit-logged and reviewed.
  Agent should almost certainly refuse to operate under break-glass. Revisit.

### OAuth2 / SMART client + token behavior
- **CTRL-1 (good):** `user/*` and `system/*` scopes are refused for public
  clients — `"system and user scopes are only allowed for confidential clients"`.
  Gate is the `application_type: "private"` flag at registration
  (`src/RestControllers/AuthorizationController.php:312`), which generates a
  client_secret and sets `client_role: user`. `system/*` additionally needs a
  JWK/jwks_uri (asymmetric) — i.e. SMART Backend Services.
- **CTRL-2 (good):** confidential (`user`) clients register **disabled**
  (`oauth_clients.is_enabled=0`) and require admin enablement before they can get
  a token (Administration → System → API Clients). Verified: token failed with
  `invalid_client` until enabled.
- **CTRL-3 (good):** the FHIR API enforces scopes **per request**. With a token
  granted Patient/Condition/MedicationRequest but NOT Observation:
  `GET /Patient/{id}` → 200 (data), `GET /Condition?patient={id}` → 200 (136
  problems), `GET /Observation?patient={id}` → **401**. Enforcement is at the
  API layer, done by OpenEMR — the agent gets it for free.
- **SEC-3 (risk):** dynamic client registration endpoint
  (`POST /oauth2/default/registration`) is **open / unauthenticated**. Standards-
  compliant (SMART), but on a public deployment it lets anyone mint client
  records. Mitigated by CTRL-2 for user clients; see SEC-4 for the gap.
- **SEC-4 (risk):** **patient-role (public) clients register ENABLED**
  (`is_enabled=1`) with no admin approval — asymmetric to CTRL-2. A patient/*
  app is live immediately on registration. Risk bounded (still needs a patient
  portal login to get a token), but the asymmetric default is worth a hardening
  note for any public deploy.

### SEC-5 (REFRAMED — intended tenancy model, not a gap) — no row-level access control
> **Reframe (2026-07-30):** initially flagged CRITICAL. After reviewing the
> requirement and OpenEMR's tenancy model, we reclassify this as the **intended
> design**, not a defect the agent must fix. See decision **D-5**. The empirical
> finding below is still accurate and worth documenting in AUDIT.md; only its
> *severity/implication* changes.
>
> - The Week 1 requirement's binding clause is *"know who is asking and enforce
>   appropriate access — not assume all users are trusted."* The phrase "a
>   physician has access to their own patients" is illustrative, not a mandate for
>   per-physician row-level walls.
> - **Tenancy boundary = the deployment, not the physician.** OpenEMR multisite
>   (`generateMultisiteBank`) gives each hospital/clinic its **own database + site**.
>   Cross-hospital isolation is by separate deployment; within a site, broad
>   physician access is intended.
> - **Real-world correctness:** physician turnover, cross-cover, on-call, ER access
>   all require any physician to reach any patient in the facility. Real EHRs
>   (Epic/Cerner) allow broad within-facility access + **audit logging + break-glass**
>   for sensitive records, rather than per-physician walls.

- **Finding:** OpenEMR enforces access at the **role** and **resource-type**
  level, but **NOT at the patient level.** Any authorized clinical user can read
  ANY patient's full chart, regardless of provider assignment or care-team.
- **Proven empirically (2026-07-30):** created two Physicians — House (owns pid 1
  only) and Watson (owns pids 2,3,4). With House's own valid `user/*` token:
  - reads his own patient (pid 1) → 200 (expected)
  - reads **Watson's** patient (pid 2 Pfeffer) → **200, full demographics**
  - reads that patient's problem list → **all 136 conditions returned**
  - `GET /Patient` (search all) → **all 20 patients visible**
- **Code confirmation:** `PatientService::search` (src/Services/PatientService.php:418)
  filters only by search params; the `providerID` join is display-only. No
  user→patient WHERE clause anywhere. `FhirPatientService` has zero
  provider/care-team/restrict logic. Only restriction knobs are *facility*-level
  (`restrict_user_facility`, `gbl_restrict_provider_facility`) — coarse, off by
  default, and not patient-granular.
- **Why it's this way:** deliberate small-clinic model ("any clinician treats any
  patient who walks in"). Not a bug in OpenEMR's context — but a **critical gap**
  for a multi-provider deploy, for HIPAA **minimum-necessary**, and for our agent.
- **Impact on architecture (reshapes D-2):** "agent inherits the user's AAA" buys
  us role + resource-type scoping **for free**, but does **NOT** give patient-panel
  scoping. If a use case needs "only MY patients," we must enforce it **in the
  agent layer** (filter the patient set the agent will act on by care-team /
  provider / encounter ownership) — OpenEMR will not do it for us.
- **This is the hospital-CTO interview answer** to "how do you know a doctor can't
  pull a patient that isn't theirs?": OpenEMR itself doesn't stop them; here is the
  control we add. Also a **compliance** finding (HIPAA minimum-necessary), not only
  security.

### Repro artifacts (local only, throwaway)
- Test physicians `drhouse` / `drwatson` (pw `DocPass123!`), Physicians role,
  panels assigned via `patient_data.providerID`. Created directly in SQL — the
  full recipe (users + users_secure.id + phpGACL manual ids + legacy `groups`
  membership + `last_update_password`) is non-obvious; see below.
- **User-creation gotchas (for reproducibility):** a valid API-login user needs
  (1) `users` row (authorized=1, active=1, uuid set); (2) `users_secure` with
  **`id` = users.id** (not auto-inc) AND **`last_update_password` set** (NULL =
  login rejected as needs-reset); (3) phpGACL `gacl_aro` (**manual id**, no
  auto-inc) + `gacl_groups_aro_map` to group 13 (Physicians); (4) a row in the
  **legacy `groups` table** (authorization group, e.g. 'Default') — without it,
  login fails with *"user not found in a group."* Password hash via container
  `php -r "echo password_hash('..', PASSWORD_DEFAULT);"`.
- Confidential PoC client enabled via `UPDATE oauth_clients SET is_enabled=1`
  (this simulated the admin-approval step). Client id/secret in `/tmp/copilot_*`
  inside the run — **PoC creds, never commit**, regenerate as needed.
- OAuth keys: `oauth_keys` was false pre-flow; the token exchange generated the
  RSA keypair (resolves half of O-7; `installed:false` marker still cosmetic).

---

## Data-quality audit (2026-07-30)

Profiled the exact data the agent reads for UC-1 (what changed) and UC-2 (labs).
20 Synthea patients. Findings drive architecture, especially the verification layer.

### Positives (solid foundation)
- **Coverage:** 20/20 patients have encounters, problems, vitals rows, and labs;
  19/20 have meds. No empty charts.
- **Coding:** **100% of problems and meds are coded**; **100% of lab results carry
  a LOINC** (`result_code`) and a human name (`result_text`). The agent can key
  reasoning off standard codes.
- **Trending is feasible** (with the right anchor, see DQ-3): e.g. Pfeffer glucose
  64→75→93 mg/dL across 2016 dated draws.

### DQ-2 (CRITICAL, drives verification design) — no ref ranges, no abnormal flags
- `procedure_result`: **0/6489 have a reference `range`; 0/6489 have an `abnormal`
  flag.** OpenEMR's own abnormality signal is entirely absent in this data.
- **Implication:** the agent CANNOT lean on the EHR to decide what is abnormal.
  It must apply **its own LOINC-keyed reference ranges + abnormality logic**, and
  that logic must be grounded/verifiable (this is literally the domain-constraint
  half of the Week-1 verification requirement). Abnormality detection is the
  agent's job, by necessity. Big architecture driver for UC-2.

### DQ-3 (CRITICAL, resolved) — lab result date is empty; anchor on order date
- `procedure_result.date` = `0000-00-00` for **all** rows (unusable).
- `procedure_order.date_ordered` is **100% populated** (3032/3032), real spread
  2016–2026. `date_collected` only ~10% populated.
- **Rule for the agent:** anchor lab timelines/trends on
  **`procedure_order.date_ordered`**, never `procedure_result.date`. Trending is
  then feasible (proven with Pfeffer glucose series).

### DQ-4 — ~26% of results are non-numeric (qualitative)
- 4813/6489 numeric; the rest are qualitative (urinalysis presence, screens).
  Agent must handle both value types; do not assume numeric.

### DQ-5 (gap) — vital signs BP / weight not reliably present — REMEDIATED (2026-07-30)
- **Root cause (confirmed in code):** OpenEMR's CDA importer (`InsertVitals` in
  `src/Services/Cda/CdaTemplateImportDispose.php`) supports `bps/bpd/weight`, but
  Synthea encodes blood pressure as a **paired/component observation** the CDA
  parser does not map. Scalar vitals (height, BMI, pulse, respiration) imported;
  **BP and weight did not**, and only **one** vitals snapshot per patient was
  created (not the per-encounter series). Re-importing (dev or non-dev) would NOT
  fix this — it is a parser/mapping gap, not the optimize flag.
- **Remediation applied:** synthetic backfill of **BP + weight across 784 recent
  encounters (2016+)** for all 20 patients, each linked to its encounter via a
  `forms` registry row (784 `form_vitals` = 784 `forms formdir='vitals'`).
  Values are **correlated to conditions**: hypertensive patients run elevated with
  a mild "not-at-goal" upward creep (e.g. Pfeffer systolic 151–156); weight derived
  from the imported BMI×height² with a gentle gain for obese patients. Deterministic
  (seeded per pid). Generator + SQL in scratchpad (`gen_vitals.py`,
  `backfill_vitals.sql`); reproducible. **Clearly synthetic, documented as such.**
- **Units note:** imported vitals are **metric** (height cm, weight kg, BMI given);
  backfill matches. OpenEMR's default UI assumes US units, so raw values may render
  oddly in the UI, but the agent reads value+unit and trends internally-consistent
  series, so this is fine for UC-2. (Minor data-quality note, not a blocker.)
- **Result:** BP and weight trending now available for UC-1/UC-2 (hypertension,
  obesity). Gap closed.
- **FHIR surfacing (found during build):** backfilled vitals did NOT appear via
  FHIR until we (1) registered each `form_vitals.uuid` in **`uuid_registry`**
  (table_id='id'), and (2) populated **`uuid_mapping`** — OpenEMR exposes each
  vital component (systolic 8480-6, weight 29463-7, BMI 39156-5, HR 8867-4, ...)
  as its OWN FHIR Observation with its own mapped uuid. Fix: run OpenEMR's own
  `UuidMapping::createAllMissingResourceUuids()` (via
  `copilot/scripts/populate_uuid_mappings.php`, executed `docker exec -u apache`;
  created 11,764 mapping rows). Any raw-SQL data insert that must surface via FHIR
  needs both steps. **Parser TODO:** BP systolic/diastolic live in the FHIR
  Observation `component[]` array (panel value is null at top level); the client's
  value extractor must read components for BP and the vitals panels.

## Build log — data-access foundation (2026-07-30)

First increment built and **proven end-to-end against live OpenEMR** (`copilot/`,
Python + httpx + pydantic; no LLM yet):
- Async FHIR client authenticates as the user (password grant, dev), pulls all 7
  resource types **in parallel**, filters labs to a recent window + cap (PERF-4),
  returns a typed `PatientContext` with **every fact carrying its `source_id`**
  (D-8). Bootstrap: `scripts/bootstrap_dev_client.sh` (register + enable client +
  write `.env`).
- **Measured (Pfeffer):** 136 problems, 18 meds, 200 labs (from 2542, capped),
  200 vitals, 115 encounters, 2 allergies. **Wall 4.0 s vs ~12 s serial** — the
  D-7 parallelization confirmed in code.
- Files: `app/config.py`, `app/schemas.py` (typed contracts), `app/fhir_client.py`
  (token + parallel reads + filtering), `run_context.py` (CLI).
- **Next build step:** the LangGraph agent + verification gate on top of this;
  resolve D-9 (reference-range table) as the verification layer is built; refine
  the vitals component parser for BP.

## Build log — agent layer (UC-1 + verification gate) (2026-07-31)

Second increment built and proven. The whole pre-visit synthesis runs as a
LangGraph graph, and the LLM is deliberately kept **out of the truth path**.

- **Graph** (`app/agent.py`): `prepare → narrate → verify → render → END`, each
  run carrying a `correlation_id` and recording per-node latency, token usage, and
  verification pass/fail counts.
  - `prepare` (deterministic): parallel FHIR pull → `compute_changes` → fact index.
  - `narrate`: the **swappable** LLM step (`app/llm.py`). `ClaudeNarrator` calls
    Claude with **forced structured tool-use** (can only return a `SummaryDraft`);
    `StubNarrator` is a faithful deterministic narrator needing **no API key**, so
    the graph + gate run in CI and eval without tokens. `get_narrator()` picks
    Claude iff `ANTHROPIC_API_KEY` is set, else the stub. Graph/gate are identical
    either way.
  - `verify`: the gate. Fails **closed** — unverifiable statements are dropped and
    logged, never shown.
  - `render`: composes the briefing, **injecting value + reference + provenance +
    source_id deterministically** from the ChangeSet (D-8), not from the model.
- **UC-1 change detection** (`app/changes.py`): finds the prior-visit boundary,
  lists new problems/meds since it, groups labs by LOINC for trends
  (`newly_abnormal` / `worsening` / `resolved` / `stable_*`), and surfaces
  `current_abnormals` + `current_criticals`. `current_abnormals` was added after
  testing showed a *stably* high creatinine (UC-2 "flag worrisome") would otherwise
  be missed by a pure delta view; `worsening` fires when an already-abnormal value
  drifts >2% further out.
- **Verification gate** (`app/summary.py`) — the graded "verification layer". The
  LLM emits structured `Statement`s (category, text, `source_ids`,
  `asserted_status`); the gate enforces three invariants against the ChangeSet:
  1. **source attribution** — every `source_id` must exist in the change-set;
  2. **domain constraint** — an asserted lab direction must match the table's
     verdict (can't call a `high` value `low`, or a `high` value `critical`);
  3. **no unsourced numbers** — integer/decimal values in a lab claim must appear
     in a cited fact (values come from the renderer, never the model).
- **Proven, offline (no key):**
  - Faithful run (Pfeffer): creatinine rendered as elevated **and** worsening,
    each line source-attributed with the deterministic value/ref/provenance;
    12/12 claims verified; full trace emitted (parallel fetch ~2 s each, 3.9 s wall).
  - Adversarial run (a narrator that invents a source, flips a direction, and
    fabricates a value): **3 of 4 claims dropped**, only the true one rendered.
- **Tests** (`tests/`, 23 passing, no live OpenEMR): `test_reference_ranges.py`
  pins D-9 boundaries (bands, sex-specificity, unit mismatch, pediatric, inverted
  HDL, target-based cholesterol); `test_verification.py` pins the gate
  (unknown source, wrong direction incl. the worsening-fact regression, invented
  integer/decimal value, fabricated critical, category mismatch, full-hallucination
  block). These are the regression guards the Week-1 rubric asks for.
- **Deps added:** `langgraph==1.2.10`, `anthropic==0.120.2`, `langfuse==4.14.2`
  (runtime); `pytest==9.1.1` (dev). Langfuse SDK present but the sink is wired when
  the self-hosted server is stood up; structured trace fields exist now regardless.
- **Known limitations / next:** (a) a lab claim citing the *wrong analyte's*
  source_id with a consistent direction (e.g. "Glucose" text over a creatinine id)
  is not yet caught — the injected evidence makes it visible, but it belongs in the
  eval set; (b) run with a real key to capture Claude token/latency baselines;
  (c) UC-3 conversational Q&A and the Langfuse server are the next surfaces;
  (d) BP `component[]` parser still TODO.

### DQ-6 (defect) — malformed placeholder values in the data
- Some qualitative results imported the literal template string **`{entry.value}`**
  as the value (units `UNK`) — a Synthea/CCDA import artifact, i.e. garbage.
- **Implication:** the verification layer / tool schemas must **validate and reject
  malformed values** (`{...}`, `UNK`) rather than pass them to the model or the
  physician. Concrete boundary-condition case for the eval suite.

### Net
Foundation is strong (coverage + coding + trendable dated labs). Two hard
constraints the architecture must absorb: **(DQ-2)** the agent owns abnormality
detection via its own reference ranges, and **(DQ-3)** temporal anchoring is
`date_ordered`. One remediation item **(DQ-5)** on vitals; one input-validation
requirement **(DQ-6)**. See also DQ-1 (import flattened `lists` med/problem dates).

## Performance audit (2026-07-30)

Measured the real FHIR reads the agent depends on, against Pfeffer (data-rich).
**Compute baseline:** openemr container sees **12 vCPU**, no mem cap (host 15.5 GB),
idle CPU <0.3%. Numbers are therefore **optimistic** — a constrained prod
container (1–2 vCPU) would be slower, especially the parallel case.

| Endpoint | KB | resources | p50 ms |
|---|---|---|---|
| Patient/{id} | 2.2 | 1 | 2947 (cold start) |
| Condition?patient | 223.7 | 136 | 1816 |
| MedicationRequest?patient | 15.2 | 18 | 2381 |
| Observation?patient | **3018** | **2542** | 3483 |
| Encounter?patient | 94.1 | 115 | 1884 |
| AllergyIntolerance?patient | 2.3 | 2 | 1627 |
| **SERIAL full context** | | | **14139** |
| **PARALLEL (6 concurrent, wall)** | | | **2989** |

### Findings
- **PERF-1 (dominant): ~1.6–2.4 s fixed per-request overhead**, independent of
  payload (Condition = 223 KB/136 in 1816 ms; AllergyIntolerance = 2 KB/2 in
  1627 ms). This is OpenEMR's heavyweight FHIR stack: per-request PHP boot +
  OAuth token validation + FHIR autoloading/serialization. First call ~3 s
  (opcache/autoloader **cold start**).
- **PERF-2: serial full-context = ~14 s** — unacceptable for Dr. A's few-second
  tolerance, and that is *before* any LLM call.
- **PERF-3: parallelizing collapses it to ~3 s** (6 concurrent, ~4.7× speedup),
  bounded by the slowest call (Observation 3.5 s). Biggest single lever.
- **PERF-4: Observation is a 3 MB / 2542-resource bomb** for ONE patient. Two
  problems: it is the parallel long-pole (3.5 s), and it is unusable raw for an
  LLM (context-window + token-cost blowout). Must be filtered server-side
  (recent window / last-N per LOINC), not fetched whole.

### Design implications (feed ARCHITECTURE.md; see D-7)
1. **Parallelize all reads** — 14 s → 3 s. Non-negotiable.
2. **Filter fat endpoints** (Observation above all): pull recent/relevant only,
   never the full 2542. Cuts latency AND token cost AND context bloat. Ties to
   DQ-3 (anchor on `date_ordered`) for "recent".
3. **Prefetch during the workflow's natural gap.** The pre-visit summary happens
   *before* Dr. A enters the room; when she opens the schedule we can warm the
   next patient's context async, so it is ready on demand. The workflow hands us
   a prefetch window — big architectural gift.
4. **Consider bypassing FHIR internally.** The ~2 s is the FHIR/OAuth stack. The
   internal service layer (`src/Services/*Service.php`) could be far faster (no
   OAuth round-trip, no FHIR serialization). Trade-off: we lose the standardized
   scope enforcement FHIR gives us (CTRL-3). Likely a **hybrid**: FHIR at the
   authz boundary, direct service reads for bulk/prefetch, or a caching layer.
5. **Keep the stack warm** (cold start ~3 s): min-instances / opcache priming in
   deployment.

## Open decisions

### D-1. Where does our dev + deployment environment live?
- **Status:** OPEN — actively deciding (see "Cloud environment options" below).
- Options on the table: local Docker Desktop; single GCE VM (Spot) running the
  compose stack; managed-service split (Cloud SQL + Cloud Run) later.
- Constraint: OpenEMR dev stack is heavy (~4-8 GB RAM realistic). GCP Always-Free
  e2-micro (1 GB) cannot run it.

### D-2. Agent authentication model — PROVEN (2026-07-30)
- **Decision:** agent authenticates **as the user** via OAuth2 (authorization-code
  in prod / password grant for PoC), inheriting the user's identity + scopes.
  **Not** client-credentials (that is app-level system access = god-mode, breaks
  AAA inheritance).
- **Token format:** RS256-signed **JWT**. Payload carries `sub` = user UUID,
  `scopes` = granted SMART scopes, `aud`/`iss`/`exp`. The JWT *is* the AAA vehicle.
- **Scope model:** use **`user/*`** scopes (clinician sees their whole patient
  panel), not `patient/*` (patient-facing, single-patient). `user/*` requires a
  **confidential** client (see CTRL-3).
- **Proof performed end-to-end** (see "Controls audit — OAuth2/FHIR" below):
  in-scope FHIR reads returned real data; out-of-scope read returned 401.
- **Open sub-question:** in prod, use SMART **authorization-code + EHR launch**
  (physician logs in, real session) rather than password grant. Confirm the
  agent backend can complete the auth-code flow / token refresh.
- **Scope of inheritance (see SEC-5 + D-5):** AAA inheritance gives us role +
  resource-type scoping. It does **not** give patient-level scoping — but per D-5
  that is the **intended** model (hospital = trust boundary), so the agent does
  **not** implement per-physician walls. Patient-panel logic, if used, is a UX
  relevance filter ("my scheduled patients"), not a security control.

### D-5. Access-control boundary for the agent — DECIDED (2026-07-30)
- **Decision:** the trust boundary is the **deployment / facility (the hospital
  or clinic)**, not the individual physician. The agent enforces:
  authenticated + authorized clinical role + resource-type scopes (+ facility
  scoping only if the site is multi-facility). It does **NOT** restrict by
  provider/care-team as a security control.
- **Compliance control in place of hard walls:** **audit logging** — every agent
  access to a patient is logged and attributable (HIPAA minimum-necessary via
  accountability, matching Epic/Cerner practice). Break-glass handling for
  sensitive records is a future refinement, not Week 1 scope.
- **Rationale:** requirement's binding clause is role-appropriate access + known
  identity + no implicit trust (satisfied); "own patients" is illustrative;
  physician turnover / cross-cover / on-call make per-physician walls wrong in
  the real world; OpenEMR isolates tenants per-deployment (multisite = per-DB).
- **Consequence:** "my patients / my schedule today" is a **UX relevance filter**
  for the between-rooms workflow, not an authz boundary. Revisit only if a chosen
  USERS.md persona introduces a genuine need for finer control (e.g. behavioral-
  health sensitivity), which would then be a deliberate added feature.

### D-6. Data-access boundary — DECIDED (2026-07-30)
- **Decision:** the agent is a **reader + synthesizer of the record**, NOT an
  integration engine. It does not pull from pharmacy/specialist/HIE at query time.
- **Interop is OpenEMR's job** (async/background): pharmacy via Surescripts/eRx,
  external notes via Direct (phiMail — the `phimail-service` user) + C-CDA import,
  labs via HL7 feeds. Data lands in OpenEMR first; the agent reads the
  consolidated record via **FHIR**.
- **Demo scope:** we do NOT stand up live Surescripts/HIE. Synthea C-CDA import
  simulates "already-arrived" external data. **Week 2** builds the realistic
  external-document path (ingest lab PDF + intake/referral → structured cited facts).
- **Progression:** Wk1 = synthesize structured FHIR already in OpenEMR; Wk2 =
  ingest uploaded documents. Live external pull = possible future tool, out of Wk1 scope.
- **Document extraction (Wk2, deferred):** lab-report PDFs and scanned images must
  be **extracted to structured, cited facts** (VLM/OCR), never attached whole to the
  LLM context — same context/token discipline as PERF-4. This is the core Week-2
  "Multimodal Evidence Agent" work; noted here so the Wk1 build leaves a clean
  ingestion seam for it. Ties to UC-2-extended.

### Persona — LOCKED (2026-07-30)
- **Target user: primary-care physician, pre-visit chart review** (the ~90 sec
  before each room) + in-visit conversational Q&A. Rationale in USERS.md.
- **Headline use case (user's emphasis):** read the **lab report(s) alongside the
  notes**, reconcile them, and surface the **changes that warrant worry / caution**
  (abnormal/trending labs, new problems, med issues) — not a flat data dump.

### DQ-1 (data-quality finding) — C-CDA import flattened temporal metadata
- Synthea→C-CDA import stamped every `lists` med/problem `date` = import time
  (2026-07-30), losing real start dates. **Encounters** kept real dates (Jan–Jun
  2026); lab **result** dates should be checked (likely retained).
- **Impact:** "what changed since last visit" must key off sources with real
  timestamps (encounters, lab result dates), NOT `lists.date`. Design the diff
  accordingly. Full data-quality audit pass still pending.

### D-7. Data-access performance strategy — DECIDED (2026-07-30, refined in review)
- **Stay on FHIR** for all reads (keeps auth + per-request scope enforcement,
  CTRL-3). **No service-layer bypass** — the agent is an external service and
  cannot call in-process PHP services anyway; bypassing would only reinvent an API
  and create an authz seam. (Rejected an earlier "hybrid" idea in review.)
- The ~2 s (PERF-1) is per-HTTP-request overhead; the lever is **fewer round-trips**,
  not abandoning FHIR: **(a)** parallelize reads (14 s → 3 s), **(b)** filter fat
  endpoints to a recent window (Observation; anchor on `date_ordered`, DQ-3),
  **(c)** prefetch, **(d)** optionally a FHIR **batch bundle** (many reads in one
  request) to amortize overhead, **(e)** keep the stack warm.
- **Auth lifecycle:** one OAuth2 token per user session, carried on every call;
  token expiry / idle timeout handles its lifecycle. Not a per-read concern.
- **Prefetch model (from review):** **morning batch** over today's schedule, not
  continuous reload. Freshness (if needed) via **hash-compare then refetch only
  changed patients**, else next morning cycle. Real-time cache invalidation is NOT
  needed — explicitly out of scope.

### D-8. Verification: source attribution is an orchestration property (2026-07-30)
- **Reframe (from review):** in Wk1 there is no RAG; "sources" are the structured
  FHIR records we retrieved, each with an ID. **Provenance is owned by
  retrieval/orchestration, not the LLM** — we know each record's ID because we
  fetched it.
- **Design consequence:** **render clinical values deterministically from the
  retrieved records; keep them out of the LLM's generative path.** The number comes
  straight from `Observation/{id}`; the LLM writes prose around already-attributed
  facts, it does not restate the value. Attribution is then correct by construction
  and there is no "did the model misquote the value" problem to police.
- Consistent with "rules decide, model explains." Matches the abnormality design
  (DQ-2): deterministic module computes/renders facts, LLM handles language.

### D-9. Reference-range provenance — RESOLVED (2026-07-31)
- DQ-2: data has no ranges/flags, so the agent owns abnormality via a **LOINC →
  normal-range** table handling **units, age, and sex**.
- **Resolved as** `copilot/app/data/reference_ranges.json` + `reference_ranges.py`:
  - **Grounded in real data.** LOINC codes/units were pulled from the actual
    `procedure_result` rows loaded (not guessed), so every code in the table is a
    code we actually receive. Same analyte under serum vs blood LOINC both mapped.
  - **Unit-checked:** a value is classified only if its unit matches the table
    (with aliases); mismatch → `unit_mismatch`, left unclassified. No silent
    metric/imperial errors.
  - **Sex-aware:** creatinine, hemoglobin, hematocrit, RBC carry male/female rows.
  - **Age-aware, fail-safe:** patients < 18 → `pediatric` (adult ranges refused,
    not misapplied).
  - **Direction-aware:** `bidirectional` (electrolytes), `higher_better` (eGFR,
    HDL — only a low flag), `lower_better` (LDL/chol/TG — only a high flag).
  - **Provenance carried per analyte** and surfaced in the rendered evidence.
    `_meta.sourcing_policy` states these are conventional adult intervals and that
    production MUST substitute the performing lab's own ranges when available —
    this table is the single audited place an abnormality decision is made.
  - **Non-numeric (DQ-4) and unknown-LOINC** both resolve to explicit statuses
    (`non_numeric` / `no_range`), never a false "normal". Questionnaire scores and
    urine dipsticks correctly fall to `no_range`.
- Proven on Pfeffer (female, b.1951): coherent CKD signal — creatinine ~1.95 vs
  female ref 0.59–1.04. Boundary behavior pinned in `tests/test_reference_ranges.py`.

### D-3. Agent UI surface
- **Status:** OPEN. Likely a separate modern surface (not grafted into Angular
  1.8), embedded into the OpenEMR patient view. Decide alongside deployment.

### D-4. Synthetic clinical data source — RESOLVED (2026-07-30)
- **Decision:** use OpenEMR's **built-in Synthea importer** (ships in the flex
  dev image's devtools), not hand-rolled inserts or the FHIR transaction API.
  Data lands via CCDA through `contrib/util/ccda_import/import_ccda.php` — the
  same validated ingestion path real C-CDA documents use, so it is structurally
  realistic rather than faked rows.
- **Command (reproducible):**
  ```bash
  # from host (Git Bash): MSYS_NO_PATHCONV=1 stops Git Bash mangling /root/... into a Windows path
  MSYS_NO_PATHCONV=1 docker exec development-easy-openemr-1 /root/devtools import-random-patients 20
  # optional 3rd arg "false" = non-dev import (slower, more thorough); default is dev mode
  ```
  Implemented by `importRandomPatients()` in `docker/flex/utilities/devtoolsLibrary.source`;
  dispatched by `docker/flex/utilities/devtools` under `import-random-patients`.
  First run downloads Synthea JAR + openjdk17 JRE (~50 MB) into `/root/synthea`;
  generates into `/tmp/synthea/output/ccda`. ~6.7 s/patient.
- **Result loaded (20 patients):** 877 encounters, 758 problems, 122 meds /
  122 prescriptions, 28 allergies, **6,489 lab/procedure results**, 312
  immunizations. (Synthea labs land in `procedure_result`; `form_observation`
  stays 0 — not a gap.)
- **Gotcha:** `/root/devtools` is root-only (`-r-x------ root`); run via
  `docker exec` (default root user). On Git Bash the `MSYS_NO_PATHCONV=1` prefix
  is mandatory or the container path is rewritten to `C:/Program Files/Git/...`
  and exec fails with code 127.
- **To scale up later:** re-run with a larger N (append, does not wipe).

---

## Tech-stack rationale (the "why", for interview readiness)

- **Why PHP:** born 2005 to run on cheap LAMP shared hosting any clinic could
  afford; PHP's shared-nothing request model is trivial to deploy and hard to
  crash. 20 years + ONC certification make a rewrite economically impossible, so
  OpenEMR modernizes *in place* — hence the `src/` vs `library/` split.
- **Laminas + Symfony:** the modernization target (DI, PSR, Doctrine).
- **MySQL/MariaDB via Doctrine DBAL:** ubiquitous LAMP database; Doctrine is the
  modern access layer, ADODB a legacy shim kept alive for old code.
- **Twig + Smarty:** modern + legacy template engines coexisting (same story).
