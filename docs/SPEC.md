# BIZ OS Platform — Claude Code Build Spec

Nov 6, 2026 · @Peter Hemmingsen

## What this is and how to use it

BIZ OS is a platform for AI-run small businesses, built from reusable parts: a business-agnostic **kernel**, **capabilities** (skills with code), **ports** (adapters to outside systems), **role packs** (roles as data) and **industry packs** (one trade's vocabulary and templates). Each business is a thin **composition** that picks parts and adds its own configuration. The first composition is **HIVE**, for a solo-run commercial fit-out business; its operating model and goals are in the [BIZ OS spec](https://claude.ai/artifact/VfriHyg6XbUx743M3P7167). This doc replaces that spec's architecture and build plan.

The platform is local-first: a Llama-family model on the Mac mini does most of the work, and a cloud model (Claude) handles the few high-stakes reasoning tasks.

**Using this spec with Claude Code:**

1. Create an empty repo `biz-os`, copy the CLAUDE.md section into its root, and save this doc as `docs/SPEC.md`.
2. Run Claude Code in the repo and paste the kickoff prompt for Milestone 0.
3. Each milestone ships one reusable part and ends when its acceptance tests pass in **both** `apps/demo` (a fake business) and `apps/hive`. Commit and deploy to the Mac mini per milestone.
4. Reuse before you write: check the harvest list in each milestone; never add plumbing a listed library already provides.

## What we harvested

Nine comparable platforms were studied on 6 November 2026. None does everything BIZ OS does, but each does one thing better than anyone else. This version takes the best idea from each and puts it in a specific place in the kernel. Ideas are re-implemented in our own code; where a project is MIT-licensed and worth using directly, that's noted.

| Source | What it does best | What BIZ OS takes | Where |
| --- | --- | --- | --- |
| [Paperclip](https://github.com/paperclipai/paperclip) (MIT, open source) | Runs a company of agents: org chart, goals, heartbeats, budgets, governance | Heartbeat queue with coalescing; a monthly budget per role that stops the role at its limit; every task traces to a goal; owner can pause or retire any role; exportable company templates with secrets scrubbed | Kernel: heartbeats, budgets, goals; `biz export` |
| [FlowWink](https://github.com/magnusfroste/flowwink) (open source) | Agent-operated business with a strong trust architecture | Evidence-bound completion (work counts as done only when the activity log proves it); staged operations awaiting approval; composite actions inherit the strictest inner gate; an identity ladder for customer self-service; a follow-through sweep for unfinished multi-step work; loud alarms for silent scheduled-job failures; a two-tool MCP dispatch mode | Kernel: approvals, evidence, identity, health, gateway |
| [N1X](https://github.com/n1x-technologies) (Workspace commercial; Cortex MIT) | Vertical OS with a cited memory and a closed loop | Cited memory where every recall names its source; reversible memory writes with draft-then-curate promotion; the closed loop (compare what happened with what should have) | Kernel: memory; learning capability |
| [company-os](https://pypi.org/project/company-os/) (MIT) | Business functions with numbers-driven goals | Scorecards (target vs actual, red/amber/green); management suggestions the owner approves before they run; per-flow caps and kill switch; "learned" notes saved at the end of each run; a `doctor` command | Kernel: goals, suggestions; CLI |
| [Odoo 19](https://www.odoo.com/documentation/19.0/applications/productivity/ai/agents.html) | Modular ERP with configurable agents | Agent = instructions + topics (each with its allowed tools) + sources; the AI picks among tools, but tools enforce the rules in code; module dependencies declared in a manifest | Role pack format; tool registry |
| ERPNext / Frappe (GPL, study only) | Metadata-driven app framework | Install, uninstall and fixtures per pack; data-driven settings rather than code | Pack loader |
| [Sintra](https://sintra.ai) | One shared business "brain" for every helper | One shared memory every role reads, with a profile per brand or business; proactive daily suggestions. Its weakness, context lost between helpers, is what BIZ OS's job record solves | Kernel: memory |
| [Lindy](https://www.lindy.ai) | Trigger-and-action agents with human checkpoints | Ask-for-confirmation on any side-effecting step; escalation triggers by confidence, keyword or unrecognised intent; voice agents for phone calls | Policy; role pack `escalate_when`; later voice capability |
| Kortix | One shared machine and context for all agents | Every role sees the same records and history; no per-agent silos | Kernel records + memory |

## Architecture

&#91;embedded content: BIZ OS architecture · 5 layers + ports\]

**The reuse rule:** if a piece would make sense in a plumbing firm or a law firm, it belongs in the kernel or a capability. If it only makes sense in one trade, it goes in an industry pack. If it only makes sense in one company, it's configuration in that company's composition. Parts talk only through packets and published tools; outside systems only through ports.

## Repo layout

One monorepo, one `uv` workspace for Python and one `pnpm` workspace for the web app. Every folder under `kernel/`, `capabilities/`, `ports/`, `roles/` and `industries/` is an independently versioned package.

```text
biz-os/
├── CLAUDE.md
├── docs/SPEC.md
├── kernel/                      # biz-kernel: no business, industry or company words
│   ├── records/                 # parties, people, sites, jobs, tasks, documents, messages
│   ├── memory/                  # cited notes, sources, recall, draft→curated, undo
│   ├── packets/                 # bus, schema registry, acks, timeline
│   ├── goals/                   # goal tree, readings, scorecards
│   ├── policy/                  # policy engine, staged operations, strictest-gate rule
│   ├── approvals/               # approval queue, version pinning, autonomy ladder, suggestions
│   ├── heartbeats/              # coalescing wake-up queue, follow-through sweep
│   ├── budgets/                 # cost events, per-role monthly limits
│   ├── evidence/                # required evidence per task type, completion checks
│   ├── workflows/               # DBOS wrappers, SOP runner
│   ├── tools/                   # tool registry, side-effect classes, allow-lists
│   ├── models/                  # ModelPort, router, structured output, eval harness
│   ├── identity/                # identity ladder, signed links, portal sessions
│   ├── health/                  # schedule and heartbeat health, silent-failure alerts
│   ├── gateway/                 # MCP server, dispatch mode
│   ├── loader/                  # pack loader, requires checks, install/uninstall, fixtures
│   ├── cli/                     # biz load | doctor | eval | export | import
│   ├── auth/  audit/  api/
├── capabilities/                # biz-cap-*: business-agnostic skills
│   ├── intake/  comms/  booking/  scheduling/  voice_capture/
│   ├── documents/  quoting/  procurement/  invoicing/  payables/
│   └── cash/  planner/  learning/  growth/  portal/
├── ports/                       # biz-port-*: one adapter per outside system
│   ├── accounting_xero/  mail_m365/  mail_google/  mail_smtp/
│   ├── telephony_twilio/  sign_docuseal/  storage_local/
│   └── model_ollama/  model_claude/  stt_whisper/
├── packets/                     # JSON Schemas, one file per packet type and version
├── roles/                       # role packs (data only)
│   ├── receptionist/  estimator/  project_manager/  bookkeeper/
│   └── account_manager/  service_desk/  chief_of_staff/
├── industries/                  # industry packs (data, templates, ext_ tables, skills)
│   └── commercial_fitout/
├── templates/                   # exported composition templates (biz export)
├── web/                         # Next.js shell; screens contributed by capabilities
├── apps/
│   ├── demo/                    # fake business used by every test
│   └── hive/                    # composition.yaml, policy.yaml, goals.yaml, seed data
├── deploy/                      # docker-compose.yml, Caddy, cloudflared, backups
└── tests/                       # cross-part contract and invariant tests
```

## Kernel

The kernel knows nothing about any business. It provides records, a cited memory, packets, goals, policy and approvals, heartbeats, budgets, workflows, tools, models, the pack loader, identity, health, auth and audit. Stack: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, Postgres 16 with pgvector, DBOS Transact for durable workflows and schedules.

### Records

Generic tables every business needs. Ids are UUIDv7; times are UTC.

```sql
parties(id, kind, name, legal_id_scheme, legal_id, domain, created_at)      -- kind: organisation | person
people(id, party_id, full_name, title, email, phone)
relationships(id, from_party, to_party, kind)                               -- client | supplier | subcontractor | partner
sites(id, party_id, label, address, geo, notes)
jobs(id, client_party_id, site_id, kind, stage, owner_user_id, value_amount, currency, opened_at, closed_at)
tasks(id, job_id, title, due_at, assignee, status, source_packet_id)
documents(id, job_id, kind, title, uri, sha256, version, supersedes_id)
messages(id, job_id, party_id, channel, direction, subject, body, received_at)
notes(id, job_id, author, body, source)                                     -- source: voice | typed | system
```

**Extensions:** a pack adds data to a record through its own table keyed one-to-one by the record id, e.g. `ext_fitout_job(job_id pk, floor_area_m2, lease_end, base_build_standard)`. Packs never alter kernel tables.

### Packets

The only way parts pass information. A packet is an immutable, typed, versioned row on the job timeline.

```sql
packets(id, type, schema_version, job_id, from_part, from_sop, to_parts text[], summary,
        payload jsonb, evidence jsonb, open_questions jsonb, supersedes_id,
        approved_by, created_at)
packet_acks(packet_id, part, status, note, at)                -- status: accepted | returned
```

`publish()` validates the payload against `packets/<type>/v<n>.json`, writes the row, and triggers every subscribed SOP through DBOS. A packet not acknowledged within its window raises an escalation to the Chief of Staff role.

### Policy, approvals and autonomy

- `policy.yaml` per composition: objective, limits, authority, hard rules.
- `policy.check(action) -> Allow | NeedsApproval(approver) | Deny(reason)` runs before every tool call with side effects.
- **Staged operations:** an action needing approval is saved as a pending operation with its exact inputs; approving it runs exactly that, once. Editing it clears the approval.
- **Strictest gate wins:** a composite action (a SOP step that calls several tools) takes the strictest gate of anything inside it; it can never bypass an inner tool's approval.
- **Autonomy ladder** per decision type: 1 remember and draft, 2 recommend, 3 act and report, 4 silent. Promotion bars as in the [BIZ OS spec](https://claude.ai/artifact/VfriHyg6XbUx743M3P7167); any bad outcome demotes one level. Trust levels survive pack upgrades.

### Memory

One shared, cited memory every role reads, so knowledge isn't siloed per role or per chat.

```sql
memory_sources(id, kind, uri, sha256, captured_at)            -- email, document, voice note, SOP, web page, job record
memory_notes(id, scope, type, title, body, status, embedding vector, created_by, created_at)
                                                              -- scope: business | job:<id> | party:<id> | role:<name>
                                                              -- status: draft | curated | retired
memory_cites(note_id, source_id, quote, locator)              -- every note names where it came from
memory_links(from_note, to_note, kind)
memory_writes(id, note_id, before jsonb, after jsonb, actor, at) -- every write reversible
```

- `memory.recall(query, scopes) -> list[CitedExcerpt]` returns the few cited excerpts that answer the question, never a whole dump.
- Roles write **drafts**; drafts become **curated** only by owner approval or a curation SOP. Any write can be undone.
- Sources are never edited; notes point back to them.
- A role pack declares which scopes it may read (its **sources**, in Odoo's terms).

### Goals and scorecards

```sql
goals(id, parent_id, owner_role, key, target, period, unit)    -- mission → function goals → role goals
goal_readings(goal_id, value, at, source)
```

Every task, SOP run and packet links to a goal. The daily brief shows a scorecard: target, actual, trend, and red, amber or green. The planner ranks actions by expected effect on goals.

### Heartbeats and follow-through

Each role has a heartbeat cadence (for example every 15 minutes, or hourly) and event triggers. Wake-ups sit in a Postgres queue that **coalesces** duplicates, checks the role's budget, then runs the due SOPs. A **follow-through sweep** each heartbeat resumes any staged or multi-step work left unfinished, so nothing is forgotten.

### Budgets

Every role has a monthly cost budget (model spend plus paid tool calls) in `policy.yaml`. Every call writes a cost event. At 80% the owner is warned; at 100% that role stops until the owner raises the limit or the month rolls over. Cadence and reasoning tier are per-role dials, so cost is tuned without code changes.

### Evidence-bound completion

A task, objective or goal step can only be marked done when the activity log holds machine evidence: the packet published, the document signed, the payment received. An agent saying "done" is not proof. Each task type declares its required evidence.

### Suggestions

The planner and the learning loop don't change things directly; they create **suggestions** (a proposed action or an SOP improvement, with reasons and evidence). The owner approves or rejects; approved suggestions run as staged operations.

### Identity ladder

Outside people reach the business through scoped links and a portal, with identity injected by the server from a verified session, never from anything the model or the request body claims:

| Rung | Who | Can do |
| --- | --- | --- |
| 0 | Anonymous caller or visitor | Make an enquiry |
| 1 | Single-purpose link holder (subcontractor, supplier) | See and act on one booking or order |
| 2 | Signed-in contact | See their own jobs, documents, invoices |
| 3 | Client company member | viewer < buyer < approver < admin, within their own company only |

### Health

Every schedule and heartbeat reports its last run. A job that should have run and didn't raises an alert in the daily brief and by text; silent failures are treated as failures.

### Workflows and the SOP runner

Every SOP is a DBOS workflow: steps checkpoint to Postgres, so a restart resumes where it left off. The runner loads a role's SOP file, checks the trigger packet, calls allowed tools, calls the model router for drafting steps, validates outputs against schemas and rules, then publishes packets or stops at an approval.

### Tools

Capabilities register tools (typed functions) with a name, JSON schema, side-effect class (`read`, `draft`, `external`, `money`) and owning capability. SOPs may call only tools on their role's allow-list. Tools with `external` or `money` side effects always pass through `policy.check`.

### Pack loader

At start-up the loader reads `composition.yaml`, resolves every part's `requires`, refuses to start if anything is missing or its version range doesn't match, runs pack migrations in their own Alembic branch, registers packet schemas, tools, SOPs and screens, and prints a manifest of what loaded.

### Auth, audit and API

- Owner sign-in by passkey; staff by passkey or magic link; outside parties through the identity ladder above.
- Append-only `audit_log` for every tool call, packet, approval, memory write, budget event and policy decision.
- REST API under `/v1` with OpenAPI; capabilities mount their routes under `/v1/<capability>`.
- **MCP gateway:** exposes tools to outside agents (Claude Desktop, Claude Code, other operators) with role-scoped tokens, read-only by default. A **dispatch mode** offers just two tools, `search_tools` and `execute_tool`, so an outside agent finds what it needs without loading hundreds of tools. Every call goes through `policy.check`.
- **CLI:** `biz doctor` validates the composition, packs, ports, schedules and budgets; `biz export` writes a composition as a portable template (roles, packs, SOPs, policy, goals) with secrets scrubbed; `biz import` loads one with collision checks.

## Contracts and formats

Five formats let parts be swapped and reused. Each has a schema in `kernel/loader/schemas/` and a contract test.

### Packet schema

```json
// packets/scope.approved/v1.json
{
  "$id": "scope.approved/v1",
  "type": "object",
  "required": ["scope_document_id", "areas", "exclusions"],
  "properties": {
    "scope_document_id": { "type": "string", "format": "uuid" },
    "areas": { "type": "array", "items": { "$ref": "#/$defs/area" } },
    "exclusions": { "type": "array", "items": { "type": "string" } },
    "assumptions": { "type": "array", "items": { "type": "string" } }
  }
}
```

Core packet set for v1: `enquiry.received`, `enquiry.declined`, `opportunity.qualified`, `meeting.booked`, `brief.confirmed`, `survey.completed`, `scope.approved`, `estimate.ready`, `proposal.ready`, `proposal.sent`, `proposal.accepted`, `contract.signed`, `programme.baseline`, `booking.confirmed`, `po.issued`, `goods.received`, `site.report`, `site.issue`, `variation.raised`, `variation.approved`, `milestone.reached`, `invoice.issued`, `payment.received`, `bill.matched`, `practical_completion`, `job.handed_over`, `service_request.received`, `escalation.raised`.

### Ports

```python
class AccountingPort(Protocol):
    async def upsert_contact(self, party: Party) -> ExternalRef
    async def create_invoice(self, inv: InvoiceDraft) -> ExternalRef
    async def create_bill(self, bill: BillDraft) -> ExternalRef
    async def list_payments(self, since: datetime) -> list[Payment]

class MailPort(Protocol):
    async def send(self, msg: OutboundMessage, idempotency_key: str) -> SendResult
    async def fetch_new(self, since: datetime) -> list[InboundMessage]

class TelephonyPort(Protocol):
    async def send_sms(self, to: str, body: str, idempotency_key: str) -> SendResult
    def parse_webhook(self, request) -> TelephonyEvent       # missed call, inbound sms

class SignPort(Protocol):
    async def request_signature(self, doc_id: UUID, signers: list[Signer]) -> ExternalRef
    def parse_webhook(self, request) -> SignEvent

class ModelPort(Protocol):
    async def structured(self, system: str, messages: list, schema: dict, tier: Literal['local','cloud']) -> dict

class SpeechPort(Protocol):
    async def transcribe(self, audio_uri: str) -> Transcript
```

Every port ships a fake implementation used by `apps/demo` and the tests.

### Role pack

```yaml
# roles/estimator/role.yaml
role: estimator
version: 1.0.0
requires: { kernel: ">=1.0,<2", capabilities: [quoting, documents, voice_capture] }
mission: Turn confirmed briefs into accurate, profitable scopes and estimates.
goals: [quote_turnaround_hours, estimate_accuracy_pct, win_rate_pct]   # keys in goals.yaml
heartbeat: { cadence: 30m, triggers: [brief.confirmed, survey.completed, variation.raised] }
budget_usd_month: 40
topics:                         # instructions + the tools allowed for each
  - name: scope_writing
    sop: sops/scope_writing.yaml
    tools: [quoting.scope_write, documents.render]
  - name: pricing
    sop: sops/pricing.yaml
    tools: [quoting.price_book_read, quoting.estimate_write, procurement.rfq_request]
  - name: variation_pricing
    sop: sops/variation_pricing.yaml
    tools: [quoting.price_book_read, quoting.variation_price]
sources: [business, "job:*", "role:estimator"]   # memory scopes it may read
consumes: [brief.confirmed, survey.completed, variation.raised]
publishes: [scope.approved, estimate.ready, variation.priced]
evidence: { estimate.ready: [document.rendered, approval.granted] }
autonomy_defaults: { scope_writing: 2, pricing: 2, variation_pricing: 2 }
model_tier: { scope_writing: cloud, pricing: cloud, variation_pricing: local }
escalate_when: [confidence_below: 0.7, margin_below_policy, unknown_trade_item]
slots: [industry.scope_template, industry.price_book_categories]
```

A role pack contains no code. **Slots** are named placeholders an industry pack fills, so the same Estimator works for fit-out, plumbing or joinery.

### Industry pack

```yaml
# industries/commercial_fitout/industry.yaml
industry: commercial_fitout
version: 1.0.0
requires: { kernel: ">=1.0,<2", capabilities: [quoting, scheduling] }
extensions: [migrations/ext_fitout_job.py, migrations/ext_fitout_site.py]
fills:
  industry.scope_template: templates/scope_of_works.md
  industry.price_book_categories: data/price_book_categories.yaml
  industry.survey_checklist: data/survey_checklist.yaml
  industry.safety_hazards: data/hazard_library.yaml
skills: [skills/fitout_glossary.md, skills/make_good.md]
signals: [data/signals.yaml]
```

### Composition

```yaml
# apps/hive/composition.yaml
name: hive
kernel: "1.x"
capabilities: [intake, comms, booking, scheduling, voice_capture, documents, quoting, procurement, invoicing, payables, cash, planner, learning, portal]
ports: { accounting: accounting_xero, mail: mail_m365, telephony: telephony_twilio, sign: sign_docuseal, model_local: model_ollama, model_cloud: model_claude, speech: stt_whisper, storage: storage_local }
roles: [chief_of_staff, receptionist, estimator, project_manager, bookkeeper, account_manager, service_desk]
industries: [commercial_fitout]
policy: ./policy.yaml          # limits, authority, hard rules, per-role budgets and dials
goals: ./goals.yaml            # mission → function → role goals with targets
memory_profile: ./memory/      # seed notes: offer, voice, price book notes, past jobs
seed: ./seed/
```

`apps/demo/composition.yaml` uses the same parts with fake ports and a generic `demo_trade` industry pack.

## Capabilities

Each capability is a package with its own tables (prefixed `cap_<name>_`), tools, packet types, screens and tests. It may depend on the kernel and on ports, never on another capability's internals; it reaches others only through packets or their published tools.

| Capability | Job | Key tools | Publishes | Ports | Built on |
| --- | --- | --- | --- | --- | --- |
| `intake` | Turn calls, forms and emails into enquiries | `intake.classify`, `intake.create_enquiry`, `intake.text_back` | `enquiry.received`, `service_request.received` | telephony, mail | twilio-python |
| `comms` | Outbox, deterministic send gate, reply ingestion, opt-outs, the owner's voice | `comms.draft`, `comms.queue`, `comms.send`, `comms.classify_reply` | `message.sent`, `reply.received` | mail, telephony | Harvey's gate and outbox patterns (MIT) |
| `booking` | Meetings and site visits in offered slots | `booking.offer_slots`, `booking.book` | `meeting.booked` | mail (calendar) | msgraph / google client |
| `scheduling` | Programmes, resource calendar, trade and delivery bookings | `scheduling.build_programme`, `scheduling.book_party`, `scheduling.capacity` | `programme.baseline`, `booking.confirmed` | mail, telephony | — |
| `voice_capture` | Voice notes and photos to structured notes | `voice.transcribe`, `voice.extract` | `note.captured` | speech, storage | faster-whisper |
| `documents` | Templates, PDFs, versions, e-sign | `documents.render`, `documents.file`, `documents.request_signature` | `document.signed` | sign, storage | WeasyPrint, DocuSeal |
| `quoting` | Briefs, scopes, price book, estimates, proposals, variations | `quoting.price_book_read`, `quoting.estimate_write`, `quoting.proposal_build` | `scope.approved`, `estimate.ready`, `proposal.ready`, `variation.priced` | — | — |
| `procurement` | RFQs, quote comparison, POs, deliveries, goods received | `procurement.rfq_request`, `procurement.po_issue` | `po.issued`, `goods.received` | mail, accounting | — |
| `invoicing` | Deposits, progress claims, final invoices, debtor sequences | `invoicing.draft`, `invoicing.issue`, `invoicing.chase` | `invoice.issued`, `payment.received` | accounting, mail | xero-python |
| `payables` | Bill matching, payment runs, supplier onboarding | `payables.match`, `payables.prepare_run` | `bill.matched`, `payment_run.ready` | accounting | xero-python |
| `cash` | 13-week cash forecast, alerts | `cash.forecast` | `cash.alert` | accounting | — |
| `planner` | Rank actions by expected value against capacity and cash | `planner.rank`, `planner.explain` | `brief.daily`, `action.recommended` | model | Claude Agent SDK |
| `learning` | Estimate vs actual, win model, duration model, autonomy promotion | `learning.update`, `learning.report` | `model.updated` | — | scikit-learn |
| `growth` | Later: prospecting and outbound (CRO-AI) | — | `opportunity.qualified` | mail | CRO-AI spec |

Screens: each capability contributes pages and approval-card renderers to the shared `web/` shell through a manifest, so the owner app assembles itself from whatever capabilities a composition loads.

**Added in this version:**

- **`portal`:** client and subcontractor self-service on the identity ladder: a client sees their own jobs, documents, variations and invoices and can approve or pay through secure links; a subcontractor sees only their own bookings. An optional portal assistant answers questions scoped to the signed-in party and never moves money.
- **`learning`, extended with the closed loop:** after each job it compares what happened with what the estimate, programme and goals said should happen, and writes cited notes to memory. A **skill curator** proposes improvements to SOPs and prompts as suggestions with evidence; nothing changes until the owner approves.
- **`planner`, extended:** ranks actions by expected effect on goals, and turns its top recommendations into suggestions on the daily brief.

## Role packs and industry packs

The SOPs come from the [SOP Library](https://claude.ai/artifact/3QWC8VeviCESULuZJ7Bjsh), rewritten without fit-out words; anything trade-specific becomes a slot.

| Role pack | Needs capabilities | Consumes | Publishes | Reusable in |
| --- | --- | --- | --- | --- |
| `chief_of_staff` | planner, cash | All packets (read), `escalation.raised` | `brief.daily`, `escalation.assigned` | Every business |
| `receptionist` | intake, comms, booking | Telephony and mail events | `enquiry.received`, `meeting.booked` | Every business that takes calls |
| `estimator` | quoting, documents, voice\_capture | `brief.confirmed`, `survey.completed`, `variation.raised` | `scope.approved`, `estimate.ready`, `variation.priced` | Any quoted-work business |
| `project_manager` | scheduling, voice\_capture, comms, procurement | `contract.signed`, `site.issue`, `goods.received` | `programme.baseline`, `site.report`, `variation.raised`, `practical_completion` | Trades, construction, events, installs |
| `bookkeeper` | invoicing, payables, cash | `contract.signed`, `milestone.reached`, `bill.received` | `invoice.issued`, `bill.matched`, `payment_run.ready` | Every business |
| `account_manager` | comms, planner | `job.handed_over`, review events | `opportunity.qualified`, check-in drafts | Any repeat-client business |
| `service_desk` | intake, scheduling, invoicing | `service_request.received` | `work_order.completed` | Maintenance, IT, facilities |

Each pack folder holds `role.yaml`, `sops/*.yaml`, `prompts/*.md`, `skills/*.md`, and `tests/` with golden inputs and expected packets.

### Industry pack: `commercial_fitout`

This is the only place HIVE's trade knowledge lives:

| Contents | Fills slot |
| --- | --- |
| Scope of works template by area and trade | `industry.scope_template` |
| Price book categories: partitions, ceilings, flooring, joinery, electrical, data, AV, acoustics, signage, FF&E, make good | `industry.price_book_categories` |
| Site survey checklist: measurements, services, access, loading dock, landlord rules, hazards | `industry.survey_checklist` |
| Hazard library for office fit-out and strip-out | `industry.safety_hazards` |
| Make-good playbook and lease clause patterns | `industry.lease_reading` |
| Signals: relocation, new lease, lease expiry, make-good, expansion, tender | `industry.signals` |
| Extension tables: `ext_fitout_job` (floor area, headcount, lease end, base-build standard), `ext_fitout_site` (building manager, lift and dock rules) | — |
| Glossary and proposal language | `industry.glossary` |

The `demo_trade` pack fills the same slots with simple generic data so every role runs in `apps/demo`.

## Model layer

Local first, cloud only where it earns its cost. All model calls go through `kernel/models/router.py`; no part calls a model directly.

```python
async def generate(task: str, system: str, messages: list, schema: type[BaseModel],
                   tier_hint: Literal['local','cloud'] | None = None) -> RoutedResult
```

**Routing rules, in order:**

1. The role pack's `model_tier` for this task sets the default.
2. Cloud if the task is on the composition's high-stakes list (default: scope writing, pricing, large proposals, lease reading, weekly planner run).
3. Otherwise local, with output forced to the JSON schema (Ollama or llama.cpp structured output).
4. If local output fails schema or rule validation twice, or self-reported confidence is low, retry once on cloud and log the escalation.
5. If the monthly cloud budget in `policy.yaml` is reached, stay local and flag the item for owner review.

**Local runtime:** Ollama (MIT) on the Mac mini, serving one Llama-family model chosen by evaluation, plus a small fast model for classification. A concurrency limit of 2 heavy jobs keeps the rest of the system responsive. **Cloud:** Claude API through `model_claude`, with prompt caching on system prompts, skills and the price book.

**Evaluation harness (`kernel/models/eval/`):** every task has a set of golden cases (input, expected output, checks). `biz eval <task> --tier local|cloud` scores each tier. During shadow mode, both tiers run on real work and the results are compared; a task moves to local only when its local score is within the threshold set per task. Re-run evals whenever the local model changes.

**Untrusted text:** web pages, emails, replies and transcripts go inside `<untrusted>` tags. Models get no tools during drafting; only the SOP runner calls tools, after validation.

## Hosting on the Mac mini

One Mac mini (Apple silicon, 48–64 GB RAM for the local model, 1 TB SSD, wired ethernet, UPS) runs everything in `deploy/docker-compose.yml`, except Ollama, which runs natively on macOS to use the Apple GPU.

| Service | Runs as | Notes |
| --- | --- | --- |
| Postgres 16 | Container | Data volume on the internal SSD |
| `biz-api` | Container | FastAPI, loads the composition at start |
| `biz-worker` | Container | DBOS workflows, SOP runner, schedules |
| `biz-web` | Container | Next.js owner, client and subcontractor pages |
| Ollama | Native macOS service | Local models on the GPU; reached from containers at `host.docker.internal` |
| Whisper (faster-whisper) | Container or native | Transcription |
| DocuSeal | Container, unmodified | E-signature |
| Caddy | Container | Reverse proxy |
| cloudflared | Container | Public webhooks and link pages only; no open router ports |
| Tailscale | Native | Owner and admin access |
| Uptime Kuma | Container | Health checks and alerts |
| Backups | launchd job | Nightly `pg_dump` + restic to B2 or R2; hourly Time Machine to an external SSD |

macOS settings: never sleep, start after power failure, FileVault on (manual unlock after a power cut; the UPS covers short ones). Missed-call text-back is also configured inside Twilio, so callers get a reply even if the Mac is down. The same compose file runs on any Linux server if the business later moves to the cloud; only Ollama's address changes.

## Invariants

Named tests in `tests/invariants/` that run in CI on every change. Claude Code writes each test before the feature it guards.

**Reusability:**

| Test | Rule |
| --- | --- |
| `test_no_business_words` | `kernel/`, `capabilities/`, `ports/` and `roles/` contain no company names, industry terms or prices (word list in `tests/banned_words.txt`; `industries/` and `apps/` are exempt) |
| `test_demo_runs_everything` | Every capability and role pack loads and passes its golden tests in `apps/demo` with fake ports |
| `test_no_cross_imports` | No capability imports another capability's modules; only kernel, ports and published tool clients |
| `test_roles_are_data` | Role packs contain no Python files |
| `test_requires_enforced` | The loader refuses a composition with a missing capability, unfilled slot or wrong version |
| `test_packet_contracts` | Every published packet validates against its schema; every consumer handles every version it declares |
| `test_port_contracts` | Every port adapter passes the shared port test suite, including the fake |
| `test_extensions_only` | Pack migrations create only `ext_<pack>_*` tables and never alter kernel or capability tables |

**Safety:**

| Test | Rule |
| --- | --- |
| `test_no_external_without_policy` | Every `external` or `money` tool call passes `policy.check` |
| `test_staged_operation_exact` | Approving a staged operation runs exactly the approved inputs, once; an edit clears the approval |
| `test_strictest_gate_wins` | A composite action never runs an inner tool whose gate is stricter than the composite's approval |
| `test_no_send_without_approval` | At autonomy levels 1 and 2, nothing leaves without a named approver |
| `test_idempotent_sends` | Retrying a send with the same key never sends twice; an uncertain send isn't retried until reconciled |
| `test_workflow_resume` | Killing the worker mid-SOP and restarting resumes from the last completed step with no duplicate packets |
| `test_follow_through` | Staged or multi-step work left unfinished is picked up by the next heartbeat's sweep |
| `test_heartbeat_coalesces` | Ten triggers for one role in a minute produce one run |
| `test_budget_stops_role` | A role at 100% of its monthly budget makes no further paid calls; the owner is warned at 80% |
| `test_evidence_bound` | A task can't be marked done without its declared evidence in the activity log |
| `test_memory_cited` | Every recalled excerpt carries a source and quote; a note without a citation can't be curated |
| `test_memory_reversible` | Every memory write can be undone; sources are never modified |
| `test_memory_scoped` | A role can't recall notes outside its declared sources |
| `test_identity_injected` | Portal and link actions take identity only from the verified session; a body claim is ignored |
| `test_company_isolation` | A contact at one client company can never see or act on another company's data |
| `test_silent_failure_alarm` | A schedule that misses its run raises an alert within one cycle |
| `test_export_scrubs_secrets` | `biz export` output contains no keys, tokens or personal data |
| `test_untrusted_fenced` | Untrusted text always appears inside `<untrusted>` in prompts |
| `test_router_budget` | Cloud model calls stop at the monthly budget |
| `test_optout_permanent` | An opt-out blocks all later sends to that address on every channel |
| `test_audit_append_only` | UPDATE or DELETE on `audit_log`, `packets` and `memory_writes` fails |

## Build milestones

Ten milestones over about 13 weeks. Each one ships a reusable part, passes in `apps/demo`, and is switched on in `apps/hive` the same week.

### M0 — Kernel and demo app (weeks 1–2)

*Accept:* records, packets, goals, policy with staged operations and the strictest-gate rule, approvals and suggestions, heartbeats with coalescing and follow-through, budgets, evidence checks, health alerts, SOP runner, tools, loader, `biz doctor`, auth and audit work; a demo role wakes on a heartbeat, consumes a packet, drafts with a fake model, stops at approval, publishes on approval, and only then marks its task done; the reusability invariants and `test_workflow_resume`, `test_heartbeat_coalesces`, `test_budget_stops_role`, `test_evidence_bound`, `test_strictest_gate_wins`, `test_silent_failure_alarm`, `test_audit_append_only` pass; the Mac mini runs the stack with backups and monitoring.

```text
Read CLAUDE.md and docs/SPEC.md. Build Milestone 0: the uv workspace; the kernel (records; packets with JSON Schema validation; goals and scorecards; policy engine with staged operations and the strictest-gate rule; approval queue with version pinning, the autonomy ladder and suggestions; heartbeat queue with coalescing and a follow-through sweep; per-role budgets and cost events; evidence-bound completion; health checks for schedules; DBOS-based SOP runner; tool registry with side-effect classes and allow-lists; pack loader with requires checks, install/uninstall and per-pack Alembic branches; passkey auth; append-only audit; /v1 API; biz CLI with load and doctor); fake ports; apps/demo with a demo_trade industry pack and one demo role; deploy/docker-compose.yml. Study Paperclip's heartbeat, budget and governance design and FlowWink's staged-operation and evidence design first; reimplement in our own code. Write the invariant tests first. Stop when M0 acceptance passes.
```

### M1 — Models, memory and evaluation (weeks 3–4)

*Accept:* `model_ollama` and `model_claude` ports pass the port suite; the router follows the routing rules; `biz eval` scores a task on both tiers; memory ingests sources, writes cited draft notes, curates on approval, recalls cited excerpts by scope, and undoes any write; `test_router_budget`, `test_untrusted_fenced`, `test_memory_cited`, `test_memory_reversible` and `test_memory_scoped` pass.

```text
Build Milestone 1: ModelPort adapters for Ollama (structured JSON output) and Claude (tool-use structured output, prompt caching); the router with model_tier, high-stakes list, validation fallback and monthly budget; the eval harness with golden cases and a shadow-mode report; and kernel/memory on Postgres + pgvector (sources, notes with citations, links, draft→curated lifecycle, scoped hybrid recall returning cited excerpts, reversible writes). Study n1x-cortex (MIT) for its citation, curation and undo design; reimplement in Python. Seed apps/hive memory from the offer, voice samples and past jobs.
```

### M2 — Intake and Receptionist (week 5)

*Accept:* a missed call to the Twilio number gets a text-back and becomes an `enquiry.received`; forms and info@ emails are classified and routed; works in demo with fake telephony. **Ship to HIVE.**

```text
Build Milestone 2: the intake capability, telephony_twilio and mail_m365 ports (plus fakes), and the receptionist role pack with SOPs for missed-call text-back, form and inbox triage, and meeting booking via the booking capability.
```

### M3 — Comms (week 6)

*Accept:* outbox with deterministic gate (unrendered tags, banned phrases, length, links, suppressions, recipient match), idempotent sends, reply ingestion and classification, opt-outs by rule before the model; `test_idempotent_sends` and `test_optout_permanent` pass.

```text
Build Milestone 3: the comms capability (draft in the owner's voice, outbox, gate, send with idempotency keys and reconciliation, inbound reply classification, opt-out and escalation rules). Study Harvey's MIT gate and handler design; reuse with its licence notice where copied.
```

### M4 — Voice capture and documents (week 7)

*Accept:* a voice note becomes a transcript and structured note on the right job; templates render to PDF; DocuSeal signature requests round-trip through webhooks.

```text
Build Milestone 4: voice_capture (stt_whisper port with faster-whisper, extraction to notes and tasks) and documents (Jinja templates to PDF with WeasyPrint, versioning, sign_docuseal port, storage_local port).
```

### M5 — Quoting, Estimator and the fit-out pack (weeks 8–9)

*Accept:* brief to scope to estimate to proposal in demo and with the fit-out pack; margin rules enforced; 5 past HIVE jobs re-estimate within 10% of the actual quoted price. **Ship to HIVE.**

```text
Build Milestone 5: the quoting capability (briefs, survey, scope, price book with import, estimate with margin policy, proposal, variation pricing), the estimator role pack with slots, and the commercial_fitout industry pack (templates, price book categories, survey checklist, hazard library, ext_fitout_job and ext_fitout_site).
```

### M6 — Money: invoicing, payables, cash, Bookkeeper (week 10)

*Accept:* milestone invoices post to Xero as drafts for approval; debtor sequences run; supplier bills match to POs; 13-week cash forecast reconciles with Xero. **Ship to HIVE.**

```text
Build Milestone 6: accounting_xero port (xero-python), invoicing, payables and cash capabilities, and the bookkeeper role pack. Bank details for suppliers can only change by a verified call-back task.
```

### M7 — Scheduling, procurement and Project Manager (week 11)

*Accept:* programme from scope; subcontractor booking links; deliveries booked; daily site reports from voice notes; variations raised and signed before work; client portal shows each client only its own jobs, documents, variations and invoices, with company roles (viewer, buyer, approver, admin); `test_identity_injected` and `test_company_isolation` pass. **Ship to HIVE.**

```text
Build Milestone 7: scheduling and procurement capabilities, the portal capability with the identity ladder (signed single-purpose links, signed-in contacts, company roles), subcontractor and supplier link pages in web/, and the project_manager and service_desk role packs. Identity always comes from the verified session.
```

### M8 — Planner, learning and Chief of Staff (week 12)

*Accept:* daily brief with scorecard and ranked suggestions; capacity and cash inputs; the closed loop compares each finished job with its estimate and programme and writes cited notes; the skill curator proposes SOP and prompt changes as suggestions with evidence; estimate-vs-actual updates the price book; autonomy promotions offered when bars are met; MCP gateway works in dispatch mode for an outside agent.

```text
Build Milestone 8: planner (goal-weighted ranking; Claude Agent SDK for the weekly deep run, local model for the daily brief), learning (closed loop, estimate vs actual, win probability, durations, skill curator, autonomy promotion), the kernel MCP gateway with dispatch mode, and the chief_of_staff and account_manager role packs.
```

### M9 — Prove reuse (week 13)

*Accept:* `biz export apps/hive` produces a template with secrets scrubbed; `biz import` of that template plus a new `trades` industry pack creates `apps/demo-plumbing` with **no code changes**, reusing kernel, capabilities, ports and role packs; `test_export_scrubs_secrets` passes. Anything that needed code moves into a shared part.

```text
Build Milestone 9: biz export and biz import (roles, packs, SOPs, policy, goals, memory profile; secrets and personal data scrubbed; collision handling). Then create apps/demo-plumbing and industries/trades from an exported template using only configuration and data. If any change to kernel, capabilities, ports or roles is required, make it generic, add a test, and record it in docs/REUSE_LOG.md.
```

## CLAUDE.md

Copy into the repo root.

```markdown
# BIZ OS

Modular platform for AI-run small businesses. Layers: kernel → capabilities → ports →
role packs → industry packs → compositions (apps/). Full spec: docs/SPEC.md.
Build one milestone at a time; a milestone is done when its acceptance tests pass in
BOTH apps/demo and apps/hive.

## Where code goes
- Would it make sense in a plumbing firm or a law firm? → kernel or a capability.
- Only in one trade? → industries/<trade>/ (data, templates, ext_ tables).
- Only in one company? → apps/<company>/ (config, policy, seed). Never code.
- Roles are data: roles/<role>/ holds YAML, Markdown and tests. No Python.
- Outside systems only through ports/; every port has a fake.
- Parts talk only through packets (schemas in packets/) and published tools.

## Commands
- uv sync / uv run pytest / uv run ruff check --fix / uv run mypy .
- docker compose -f deploy/docker-compose.yml up -d
- uv run biz load apps/demo  (prints the loaded manifest)
- uv run biz doctor apps/hive  (validates packs, ports, schedules, budgets)
- uv run biz export apps/hive / biz import templates/<name>
- uv run biz eval <task> --tier local|cloud
- pnpm --dir web dev

## Rules that never bend
- Invariant tests in tests/invariants/ come first; CI green before commit.
- No live network in tests: fake ports, fixtures, respx.
- No business, industry or company words outside industries/ and apps/.
- No model call outside kernel/models/router.py; prompts live in files.
- Untrusted text is fenced; models get no tools while drafting.
- Every external or money action passes policy.check and the approval queue.
- Sends are idempotent; uncertain sends are reconciled before any retry.
- Workflows are DBOS; every step must be safe to resume.
- Roles wake by heartbeat, spend within their monthly budget, and link work to goals.
- Nothing is done until its declared evidence is in the activity log.
- Memory is cited and reversible; roles read only their declared scopes.
- Composite actions take the strictest inner gate; staged operations run exactly once.
- Identity for outside parties comes only from the verified session.
- Planner and learning propose suggestions; they never change things directly.
- Pack migrations create only ext_<pack>_ tables.
- Reuse before writing: DBOS, Claude Agent SDK, FastAPI, Pydantic, SQLAlchemy,
  xero-python, twilio-python, msgraph, faster-whisper, WeasyPrint, Next.js,
  shadcn/ui, assistant-ui, DocuSeal (unmodified service). Keep licence notices.

## When unsure
Ask before changing a packet schema, a port interface, policy behaviour, or anything
that sends, pays or signs.
```

## Open questions

- [ ] Which local model to start with? Decide by running M1's eval on HIVE's real tasks.
- [ ] Mac mini spec: 48 GB or 64 GB RAM? Depends on the model chosen in M1.
- [ ] Microsoft 365 or Google Workspace for HIVE's mail and calendar?
- [ ] Does HIVE have a price book and past jobs to seed M5's tests?
- [ ] Which second business should M9 prove reuse against: plumbing, electrical, or another of your planned builds?
- [ ] Should the platform eventually serve other businesses as a product (which affects licences, multi-tenancy and DocuSeal)?

## Related specs

- [BIZ OS spec](https://claude.ai/artifact/VfriHyg6XbUx743M3P7167): operating model, goals, autonomy ladder, costs and risks for the first composition.
- [SOP Library](https://claude.ai/artifact/3QWC8VeviCESULuZJ7Bjsh): source for the role packs' SOPs.
- [CRO-AI build spec](https://claude.ai/artifact/9pZoC8o98C8xqNK8PfSKd5): later becomes the `growth` capability and a business development role pack.
