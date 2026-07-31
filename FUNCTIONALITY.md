# OpenEMR — Functional Overview

> Working note (not a graded deliverable). Purpose: understand what the product
> actually does before auditing or building on it. Feeds the architecture audit
> and grounds `USERS.md`.

OpenEMR is a complete **ambulatory-clinic platform**: it runs both the clinical
record (the EHR) and the business of a medical practice (scheduling, billing,
reporting). It is a ~20-year-old PHP monolith, ONC-certified, deployed in real
clinics worldwide.

The organizing spine of the whole system:

```
Patient ──has many──> Encounters (visits) ──attach──> clinical data
                                                       (notes, orders,
                                                        labs, vitals, meds)
Billing and Scheduling wrap around this core.
```

Everything hangs off a **patient**; a visit creates an **encounter**; clinical
data attaches to that encounter. This is the mental model the agent must respect.

---

## Capability map by domain

Each capability is paired with the `src/Services/*Service.php` class that
implements it. **These services are the agent's future tool surface** — typed,
structured access to the same data the UI shows, returning records with IDs
(which is what makes "every claim traces to a source" achievable).

### 1. Clinical / EHR — the patient chart (core for our physician user)

| Capability | UI module | Service layer |
|---|---|---|
| Encounters & visit notes | `interface/forms`, `soap_functions/` | `EncounterService`, `ClinicalNotesService`, `ONoteService` |
| Note signing | `interface/esign/` | — |
| Problems / conditions | `interface/patient_file/` | `ConditionService`, `PatientIssuesService` |
| Allergies | `interface/patient_file/` | `AllergyIntoleranceService` |
| Medications & prescriptions | `interface/orders/`, `eRx*.php` | `MedicationPatientIssueService`, `PrescriptionService`, `DrugService` |
| Labs & observations | `interface/orders/`, `procedure_tools/` | `ObservationLabService`, `ObservationService`, `ProcedureService` |
| Vitals | `interface/patient_file/` | `VitalsService`, `VitalsCalculatedService` |
| History (social, surgical, family) | `interface/patient_file/` | `SocialHistoryService`, `SurgeryService` |
| Immunizations | `interface/patient_file/` | `ImmunizationService` |
| Care planning | — | `CarePlanService`, `CareTeamService` |
| Structured forms (pluggable) | `interface/forms/`, `forms_admin/` | `FormService` |

### 2. Practice management — running the business

| Capability | UI module | Service layer |
|---|---|---|
| Scheduling / calendar | `interface/main/` | `AppointmentService` |
| Patient flow board (waiting room) | `interface/patient_tracker/` | `PatientTrackerService` |
| Billing & claims | `interface/billing/` | `InsuranceService`, `InsuranceCompanyService`, `PatientTransactionService` |
| Pharmacy / drug sales | `interface/drugs/` | `DrugSalesService`, `DrugService` |
| Orders & procedures | `interface/orders/`, `procedure_tools/` | `ProcedureService`, `ProcedureProviderService` |

### 3. Interoperability — talking to other systems (KEY for the agent)

- **FHIR R4 + US Core 8.0 + SMART on FHIR v2.2.0**, secured by **OAuth2 /
  OpenID Connect with granular per-resource scopes** (e.g. `patient/Observation.rs`).
  Implemented under `src/Services/FHIR/`, `src/FHIR/`, routes in `_rest_routes.inc.php`.
- Document exchange: C-CDA (`src/Services/Cda/`, `CDADocumentService`), quality
  reporting QRDA/QDM (`src/Services/Qrda/`, `Qdm/`).
- `interface/webhooks/`, e-prescribing (`interface/eRx*.php`).

### 4. Patient engagement

- Patient portal (`PatientPortalService`, `interface/patient_file/`), secure
  messaging (`MessageService`, `PortalMessagingSender`), questionnaires
  (`QuestionnaireService`, `QuestionnaireResponseService`).

### 5. Administration & security

- Access control: `src/Common/Acl/` (`AclMain`, `AclExtended`) — PHP-GACL based.
- Users / roles / facilities: `interface/super/`, `usergroup/`, `UserService`,
  `FacilityService`, `LocationService`.
- Auth: `interface/login/`, OAuth2 server, `JWTClientAuthenticationService`,
  `TrustedUserService`.
- Audit log: `interface/logview/` + logging subsystem.

---

## Tech stack (and why)

| Layer | Choice | Why it's here |
|---|---|---|
| Language | **PHP 8.2+** | 2005-era origin; PHP was *the* deploy-anywhere web language on cheap LAMP shared hosting. Clinics ran it on a box in the back office. That deployment reality still shapes the codebase. |
| Legacy backend | Procedural PHP (`library/`, 617 files) | Original architecture. Global state, `$GLOBALS`/`$_SESSION` as service locator. The repo's own CLAUDE.md calls these "antipatterns … not because they are correct." |
| Modern backend | **Laminas MVC + Symfony components**, PSR-4 (`src/`, 2115 files) | The ongoing modernization layer. Doctrine DBAL, DI, typed value objects, enums, `readonly`. New code lives here — and so should our agent. |
| Templating | **Twig 3** (modern) + **Smarty 4.5** (legacy) | Two engines coexist; check file extension. |
| Front end | **Angular 1.8 + jQuery 3.7 + Bootstrap 4.6** | Angular 1.8 is EOL — a modernization debt / audit finding. |
| Build | Webpack 5 + SASS | — |
| Database | **MySQL / MariaDB** via **Doctrine DBAL 4** (ADODB surface for legacy) | Ubiquitous with LAMP; the schema (`sql/database.sql`) is ~15.4k lines. |
| Static analysis | PHPStan level 10 (max), Rector, custom rules | Modern-code guardrails; legacy is largely baseline-excluded. |

**Why PHP, in one line:** OpenEMR was born to run on commodity LAMP hosting that
any clinic could afford in 2005, and 20 years of accreted features make a
language rewrite economically impossible. The `src/` vs `library/` split is that
history frozen in the directory tree.

---

## Local dev stack (docker/development-easy)

`docker compose up` brings up **seven services**:

| Service | Image | Purpose | Host port |
|---|---|---|---|
| `openemr` | `openemr/openemr:flex` | The app (Apache + PHP) | 8300 (http), 9300 (https) |
| `mysql` | `mariadb:11.8` | Database | 8320 |
| `phpmyadmin` | `phpmyadmin:5.2` | DB browser | 8310 |
| `selenium` | `standalone-chromium` | E2E / browser automation | 4444, VNC 7900 |
| `couchdb` | `couchdb:3.5` | Document/blob storage | 5984/6984 |
| `openldap` | `dev-ldap` | LDAP auth testing | — |
| `mailpit` | `mailpit` | SMTP catch-all (test email) | 8025 (UI), 1025 (SMTP) |

- Login: `admin` / `pass`. FHIR/REST/portal APIs are pre-enabled via
  `OPENEMR_SETTING_*` env vars. OAuth2 site address is preset to `https://localhost:9300`.
- The `flex` image auto-installs OpenEMR and seeds the base DB on first boot.

## Sample data — a real gap to plan around

- `sql/example_patient_data.sql` is **tiny**: ~14 patients, **demographics only**
  (28 lines). `sql/example_patient_users.sql` adds a few users.
- **No encounters, labs, medications, or vitals ship in the seed.** A Clinical
  Co-Pilot that reads meds/labs/history has almost nothing to read out of the box.
- **Action item:** we will need to generate/import richer synthetic clinical data
  (e.g. Synthea → FHIR import, or scripted inserts) before the agent is meaningful.
  This is both a data-quality audit finding and a build prerequisite.

---

## What this means for the agent (early read, to revisit in ARCHITECTURE.md)

1. **The `src/Services` layer is a ready-made typed tool surface** — the agent
   calls services, not raw SQL, and gets records with IDs for source attribution.
2. **SMART on FHIR + OAuth2 granular scopes is the intended, standards-based way
   in.** The "who can query patient data" problem likely has an existing answer we
   enforce against rather than invent. Whether the agent authenticates *as the
   physician* (inheriting their scopes) is a central architecture decision.
3. **Seed data is insufficient** — provisioning realistic clinical data is a
   prerequisite, not an afterthought.
