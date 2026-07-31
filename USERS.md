# USERS.md — Target User, Workflow, and Use Cases

> Source of truth for the agent's scope. Every capability in ARCHITECTURE.md and
> every feature we build must trace back to a use case here. If a capability does
> not serve a use case below, we do not build it.

## Target user

**Dr. A, a primary-care physician (family / internal medicine) in a busy
outpatient clinic.**

| Attribute | Value |
|---|---|
| Panel | ~1,500 patients, many older adults with multiple chronic conditions |
| Clinic day | 18 to 22 patients, 15 to 20 minute slots |
| Time before each room | ~90 seconds to orient to the next patient |
| Chart reality | Dense. Between visits, data arrives from labs, specialists, ED visits, and pharmacies through the EHR's own interoperability channels, and sits un-synthesized until someone clicks through it |
| Tolerance | Very low patience for anything that adds clicks. Very high sensitivity to a wrong fact: a confidently stated hallucination can harm a patient |

Dr. A is not "physicians in general." She is a specific person with a specific
constraint: she must re-orient to a complex, changed chart many times a day, fast,
and she cannot trust a tool that might invent a medication or miss an abnormal lab.

We deliberately did **not** pick an ED resident (patients are strangers with no
prior record, so a longitudinal-history agent adds little) or a hospitalist (our
data and the workflow are outpatient-shaped).

## The workflow moment

The agent enters Dr. A's day at two adjacent moments:

1. **Pre-visit (the 90 seconds before the room).** Dr. A opens the agent on the
   patient she is about to see. She needs to know what changed since she last saw
   this patient, what is abnormal or trending the wrong way, and what needs
   attention today given the reason for the visit.

2. **In-visit (during the encounter).** Questions come up that she cannot predict
   in advance. She asks them in natural language instead of leaving the
   conversation with the patient to click through five tabs.

Grounding example (real data in our instance): patient Pfeffer (pid 2), ~74,
with hypertension, prediabetes, obesity, and coronary disease, on clopidogrel,
metoprolol, nitroglycerin, simvastatin, hydrochlorothiazide, and insulin. Visits
in Jan, Feb, and June 2026. In the four months between February and June, labs
resulted, a med may have changed, and problems may have been added. None of that
is in any single note. Assembling it by hand, in 90 seconds, across five tabs, is
the pain the agent removes.

## Use cases

Each use case states the trigger, what Dr. A needs, what she does with the output,
and an explicit answer to **why a conversational agent is the right shape** rather
than a dashboard, a sorted list, or a better chart view.

### UC-1 — Pre-visit "what changed since last visit" synthesis
- **Trigger:** Dr. A opens the agent on the next patient.
- **Need:** a reconciled summary of what accumulated in the chart since her last
  encounter with this patient: new or changed problems, new or stopped meds, new
  lab results, and recent encounters (including any ED or specialist records that
  arrived through the EHR's interoperability feeds).
- **Output use:** she walks into the room already oriented.
- **Why an agent:** this requires cross-source synthesis (Conditions, Medications,
  Observations, Encounters, Documents) plus a temporal diff keyed to her last
  encounter date. It is the opening turn of a conversation, not a static report.
  Honest note: the summary *alone* could be a dashboard. It earns the agent shape
  because it is the entry point to UC-3, and because "what matters today" (below)
  requires judgment a fixed panel cannot encode.

### UC-2 — Lab report read in clinical context, with caution flags (HEADLINE)
- **Trigger:** new or recent lab results are on file, or (Week 2) a lab PDF is
  uploaded.
- **Need:** read the labs alongside the notes, problems, and current meds, compare
  each result to the patient's own trend and reference range, and surface the
  changes that warrant worry or caution. Not a flat table of numbers: the
  clinically relevant deltas and their implications.
- **Output use:** Dr. A knows, before or during the visit, which results need
  action and why.
- **Why an agent:** interpreting a lab in context is reasoning across four sources
  at once (result + trend + active meds + problem list). Example: a rising
  creatinine matters more when the patient is on an ACE inhibitor and metformin,
  because it changes renal dosing and raises a contraindication. A dashboard shows
  the number; it cannot reason about what the number means for *this* patient's
  meds. And Dr. A will ask "why" follow-ups that only a conversational agent can
  chase. This use case is also where the required **verification layer** does the
  most work: every flagged value must trace to a source, and domain constraints
  (dosage thresholds, interaction and renal-function rules) gate the output.

### UC-3 — Conversational interrogation of the chart
- **Trigger:** an unpredictable question during pre-visit or the encounter.
- **Need:** natural-language answers over the longitudinal record. Examples: "What
  were her last three A1c values?" "Did cardiology change her statin, or did she
  stop it?" "Has she been filling her clopidogrel?" "Anything in that ED visit I
  should worry about?"
- **Output use:** she gets the answer in one turn instead of leaving the patient to
  navigate the UI.
- **Why an agent:** these are multi-turn, tool-chaining questions that cannot be
  pre-computed. This is the capability a dashboard, a sorted list, and a better
  chart view genuinely cannot provide. It is the core justification for the agent.

### UC-4 — Medication safety and caution checks
- **Trigger:** a new medication is present, or Dr. A asks about safety.
- **Need:** flag interactions, and dosing concerns given current labs and problems
  (for example renal dosing given eGFR).
- **Output use:** she avoids or double-checks a risky combination.
- **Why an agent:** this cross-references meds against labs and problems and applies
  clinical rules. It is decision support that reasons, not a lookup. It directly
  exercises the **domain-constraint enforcement** half of the verification
  requirement.

### Adjacent, Week 2 extensions (noted, not built in Week 1)
- **UC-2 extended:** ingest an uploaded lab **PDF** or an external specialist
  document and turn it into structured, cited facts (Week 2 multimodal ingestion).
- **UC-5 care gaps:** surface overdue screenings and immunizations by reasoning
  over guidelines versus the record (Week 2 guideline RAG).

## Non-goals and refusals (scope guardrails)

- **No per-physician patient restriction.** Per decision D-5, the trust boundary is
  the clinic/hospital deployment, not the individual physician. The agent does not
  wall patients by provider. (Turnover, cross-cover, and on-call make that wrong.)
- **No live external integration.** Per D-6, the agent reads the consolidated
  record; it does not phone the pharmacy or an HIE at query time. Interoperability
  is the EHR's job.
- **Read and synthesize only in Week 1.** The agent does not write to the chart or
  place orders. It is decision support with the physician in the loop.
- **Never operates under break-glass** (SEC-2). Any break-glass session is flagged
  in the agent's audit log, never used as the agent's own access mode.
- **Not a diagnosis engine.** It surfaces and reasons over what is in the record; it
  does not make autonomous clinical decisions.

## Access and trust context (how the user's identity binds the agent)

- The agent authenticates **as Dr. A** through OpenEMR's OAuth2 / SMART flow and
  inherits her identity and scopes (decision D-2). It can only see and do what she
  can.
- Access is enforced by OpenEMR at the **role** and **resource-type** level; the
  **facility** is the outer boundary (D-5).
- **Every claim the agent makes traces to a source record** (a FHIR resource ID).
  This is both the verification requirement and the compliance control: access is
  logged and attributable, satisfying HIPAA minimum-necessary through
  accountability.
