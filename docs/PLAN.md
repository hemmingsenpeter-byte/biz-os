# BIZ OS: Building reliable, trainable AI employees (v2.1)

Nov 12, 2026 · @Peter Hemmingsen

This is the build plan. `docs/SPEC.md` stays the architecture reference; where the two disagree on build order, this plan wins. v2.1 is v2 reconciled with what is already in the repo (see "What changed from v2").

## What changed from v2

v2 was written as if nothing existed. The repo already holds M0 to M6 of `docs/SPEC.md`, built against fakes and never deployed. v2.1 keeps v2's thesis and fixes the places where it disagreed with the code or with itself.

| v2 said | Problem | v2.1 |
| --- | --- | --- |
| Workflow engine is LangGraph with PostgreSQL checkpoints | The repo runs every SOP on DBOS, and CLAUDE.md makes that a rule | DBOS. LangGraph is dropped |
| Tailscale is deferred until the second appliance | `deploy/` already keeps `/v1` private behind Tailscale; without it the owner has no remote access to approvals | Tailscale is in from the first install |
| One Office Administrator, three autonomy levels, a permissions YAML | The repo has a receptionist role pack, a four-level ladder per decision type, and `policy.yaml` | Mapped below; no new formats |
| Durable execution, idempotency, verification and backups arrive in Stage 3 | Stage 2 already sends real email. A retry without idempotency, or a lost disk without a backup, would hit real customers | These are preconditions of Stage 2 (most are built). Stage 3 only proves them under failure |
| Five hours a week saved, from enquiry handling | Unchecked against volume. At 6 minutes an enquiry, five hours needs more than 50 enquiries a week | Stage 0 measures weekly volume. If volume times baseline is under five hours, the target or the scope changes before Stage 1 |
| Pass-rate gates on a 100-case set | Cases used to tune prompts can't also be the gate, and the awkward cases rarely appear in history | A held-out split, plus written awkward and injection cases |
| "Nothing else gets built until the number holds" | The repo has already built estimator and bookkeeper roles, and SPEC's next milestone is M7 | M7 to M9 are frozen until the Stage 2 gate passes. Built roles stay in the repo, switched off at Hive |
| Replies go only to the original sender | Sender verification is always false today (build log, defect #3), so "original sender" can be a spoofed address | Listed as a Stage 2 precondition; needs a mail port change the owner must approve |

One thing to confirm: v2 calls the first customer Hive Property Services, a trade business. `apps/hive` and SPEC describe HIVE as a commercial fit-out business. This plan assumes they are the same company.

## Product thesis and first customer

BIZ OS sells a supervised digital worker to a small-business owner who will never see a prompt, model or API. The first build is one Office Administrator for Hive, running on a Mac mini.

The first employee does four things: reads inbound enquiries, classifies them, prepares the customer record, and drafts the reply. Every external action waits for owner approval.

The target is five hours saved per week for the owner, measured rather than estimated. Nothing else gets built until that number holds for four consecutive weeks.

An AI employee is a versioned configuration, not a personality in a prompt. It can be tested, copied to another business, rolled back and restored from backup.

An employee that is 95% reliable per step completes a five-step task correctly only about 77% of the time (0.95^5). Reliability has to come from validation between steps, not from a better model alone.

## How the plan maps to the repo

| Plan term | In the repo |
| --- | --- |
| Office Administrator | `roles/receptionist` with the `intake`, `comms` and `booking` capabilities |
| Capability (the unit that earns autonomy) | A decision type in `policy.yaml`, for example `reply_drafting`, `reply_sending`, `meeting_booking` |
| Observe / Supervised / Limited autonomy | Ladder levels 1 / 2 / 3. Level 4 (silent) is not used |
| Tool gateway and policy service | Tool registry with side-effect classes, role allow-lists and `policy.check` |
| Approval bound to the exact payload | Staged operations (`inputs_sha256`, version pinning; an edit clears the approval) |
| Workflow engine | DBOS SOP runner |
| Knowledge store | `kernel/memory` (cited, scoped, reversible) on pgvector |
| Model gateway | `kernel/models/router.py` with `model_ollama` and `model_claude` |
| Test set and test runner | `roles/<role>/tests/eval/<task>/` and `biz eval <task> --tier local|cloud|shadow` |
| Business HQ | `web/`: the owner console (passkey sign-in, approvals, tasks) |

## Architecture

The model proposes; conventional software decides, executes and records. The model never holds credentials, never calls an external system directly, and never writes to the approval or audit tables.

A request flows in one direction: trigger, workflow step, model proposal, deterministic validation, policy check, approval if required, execution through the gateway, verification, audit record.

The model can decide an invoice looks suspicious. Software checks the arithmetic, the approval status and whether it has already been paid.

## Autonomy

Autonomy is granted per decision type, not per employee.

| Level | What the employee does | How it is earned |
| --- | --- | --- |
| 1 Observe | Recommends only; owner does the work | Default for every new decision type |
| 2 Supervised | Prepares the action; owner approves each one | Passes the decision type's held-out test set |
| 3 Limited autonomy | Executes low-risk actions; owner reviews a sample | Sustained production record plus explicit owner sign-off |

Some actions never become autonomous, whatever the record: payments, refunds, deletions, bank-detail changes and anything contractual. A decision type drops one level automatically after a serious error or a failed regression run.

`policy.yaml` is an upper bound: a promotion can never lift a decision type above the level the file names. Hive starts as it is configured today:

```yaml
autonomy:
  default: 2
  reply_drafting: 2
  reply_sending: 2
  missed_call_text_back: 2
  meeting_offer_sending: 2
  meeting_booking: 2
limits:
  max_amount: { default: 0 }
  over_limit: deny
```

Creating a customer record stays at approval until duplicate matching is proven, because bad records are expensive to clean up.

## Training system

Training turns the owner's corrections into versioned rules and test cases. It is built in order of signal per unit of effort.

1. **Corrections (build first).** The employee drafts, the owner edits and approves. The system stores the draft, the edit, a one-line reason and a severity tag (cosmetic, factual or policy), then proposes a rule as a suggestion. Nothing changes until the owner approves it.
2. **Documents.** The owner uploads procedures, templates, price lists and policies as memory sources. Every document has an owner and a review date so stale prices do not reach customers.
3. **Historical cases.** Past enquiries with known correct outcomes become the test set.
4. **Demonstration (defer).** Needs screen or action capture. Deferred until corrections and documents work.

Every approved rule produces three things: a new SOP or prompt version, at least one test case, and a regression run across the existing test set. A rule that breaks older cases is shown to the owner as a conflict, not applied silently.

Example. A customer writes that a tap repaired last week is leaking again. The employee drafts an offer of a free return visit. The owner corrects it. The resulting rule:

> Before offering a warranty repair, retrieve the original job, establish whether the fault falls within the warranty terms, and escalate ambiguous cases.

The employee never rewrites its own instructions. It proposes; the owner approves; the change is versioned and reversible.

## Reliability and security

Reliability comes from the workflow around the model, and security comes from the gateway, never from instructions to the model.

| Requirement | What it prevents | State |
| --- | --- | --- |
| Durable execution | Losing tasks when software crashes or the Mac restarts | Built (DBOS; `test_workflow_resume`) |
| Idempotency keys on every external action | Duplicate jobs, invoices or emails after a retry | Built (send ledgers, reconcile before retry) |
| Permissions enforced in the gateway | An employee reaching systems outside its role | Built (allow-lists, `policy.check`) |
| Approval bound to the exact payload | Recipient, amount or message changing after approval | Built (staged operations) |
| Post-action verification | Reporting success when the external application failed | Built as evidence-bound completion |
| Untrusted-input handling | Inbound content steering the employee | Partly built: text is fenced, models get no tools; one injection case per task |
| Kill switch | One control that pauses every employee | Built (`test_kill_switch`; `biz pause`, `/v1/control`) |
| Sender verification | Replying to a spoofed "original sender" | Not built; needs a mail port change |

### Untrusted input

Every inbound email, attachment and web page is written by someone outside the business and may contain instructions aimed at the model. For an email-reading employee this is the main threat.

- Inbound content is treated as data. It can change what the employee proposes, never what it is permitted to do.
- A task triggered by external content cannot send to a new recipient, share files or change records without approval, at any autonomy level.
- Replies go only to the verified original sender or to addresses already on record.
- Retrieved knowledge and inbound content are kept apart in the prompt and labelled by source.
- The test set includes injection attempts, such as an email asking the employee to forward the customer list.

### Audit

Every significant action writes one append-only record: employee, action, trigger, evidence, SOP version, model version, policy decision, approver and verified result.

The log must answer four questions: what happened, why it was permitted, who authorised it, and what confirms the outcome. It records inputs, decisions and observable actions, not model reasoning.

Append-only is enforced today by a Postgres trigger. Hash-chaining the records, so tampering by someone with database access is detectable, is a Stage 3 item.

## Measurement

An employee earns autonomy from a measured record, never from a model's stated confidence.

| Metric | Definition |
| --- | --- |
| Test pass rate | Share of the decision type's held-out test cases matching the owner-approved outcome |
| Approval without edit | Share of production proposals the owner approved unchanged |
| Edit severity | Each owner edit tagged cosmetic, factual or policy; factual and policy edits count as errors |
| Serious error count | Actions that reached a customer or record and had to be reversed |
| Time saved | Tasks completed multiplied by the owner's measured baseline time per task, minus review time |
| Cost per task | Model and compute cost divided by tasks completed |

The baseline matters: time the owner on 20 tasks before the employee starts, and count a normal week's enquiries, or "time saved" is a guess.

The test set has two parts. Historical cases come from Hive's real enquiries. Written cases cover what history rarely contains: repeat complaints, missing details, abusive emails, warranty disputes, duplicates, refund requests and injection attempts. A fixed share is held out and never used to tune prompts; gates are scored on that share only. The set reruns on every change to the model, SOPs or tools.

A pass rate on a small set is a rough number: 95 of 100 is consistent with a true rate anywhere from about 89% to 98%. The set grows from production corrections, so the gates tighten with use.

Proposed promotion gates, to be tuned at Hive:

- Level 1 to 2: at least 95% pass rate on at least 50 held-out cases for that decision type.
- Level 2 to 3: at least 200 production tasks, at least 95% approved without factual or policy edits, zero serious errors in the last 100, and owner sign-off.

## Appliance operations

The owner sees a health light and an approvals queue; everything below is the platform's job.

### Local and cloud models

The plan assumes a local model does most of the work. That is a hypothesis to test in Stage 1, not a design fact.

- Run every task's test set against the local model and the cloud model. Record pass rate, latency and cost for each.
- Route a task to the local model only where it meets the promotion gate. Classification and extraction are the likely candidates; customer-facing drafting may not be.
- Cloud use is opt-in per data class. The owner sees plainly which data leaves the building and to whom.
- Pin model versions. A model change is a release and reruns the full test set.

### Running the box

| Function | Implementation |
| --- | --- |
| Installation | Reproducible installer and configuration templates |
| Startup | Services restart automatically after reboot or power loss |
| Remote access | Tailscale; public webhooks only through cloudflared; no open inbound ports |
| Security | FileVault, least privilege, secrets in the keychain, a dedicated non-admin service account |
| Backups | Nightly encrypted backups to separate storage, with a monthly restore test |
| Updates | Signed releases, staged rollout, rollback |
| Monitoring | Health checks and alerts sent to the operator, not only the owner |
| Support access | Time-limited and owner-authorised, logged in the audit trail |
| Hardware failure or theft | Documented path to restore onto a replacement unit, with a target recovery time |

The dashboard distinguishes fully operational from locally operational. If the internet drops, tasks queue and nothing is lost.

## Stack

| Layer | Choice | State |
| --- | --- | --- |
| Business HQ | Next.js / React (`web/`) | First slice built: sign-in, approvals, tasks |
| Backend, tool gateway, policy and approvals | Python / FastAPI | Built |
| Workflow engine | DBOS on PostgreSQL | Built |
| Database, retrieval and audit | PostgreSQL with pgvector | Built |
| Inference | Ollama locally, Claude through `model_claude` | Built against mocks; no local model evaluated yet |

| Deferred | Add it when |
| --- | --- |
| OpenClaw | You need its chat channels. Run it sandboxed, with no credentials, calling the tool gateway like any other client |
| n8n | Direct connectors exceed about five and maintenance becomes the bottleneck |
| Langfuse | The built-in test runner can no longer answer why a case failed |
| Temporal | DBOS can no longer carry workflows that run for days across many customers |
| Frappe or Twenty CRM | The customer has no CRM. Otherwise connect to the one they already use |

## Staged plan

Each stage ends on a number or a demonstrated recovery, not on features shipped. Thresholds are proposals to tune at Hive.

| Stage | Still to build | Exit gate |
| --- | --- | --- |
| 0. Baseline | Label 100 historical Hive enquiries with the correct outcome; write the awkward and injection cases; time the owner on 20 live tasks; count enquiries in a normal week | Test set (with held-out split) and time baseline exist; weekly volume times baseline time is at least five hours, or the target or scope is re-set |
| 1. One employee, shadow mode | Mac mini install (compose stack, Ollama, Tailscale, backups, monitoring); Business HQ with approvals queue, health light and kill switch; `biz eval` on both tiers; live Microsoft 365 read with all sends blocked | At least 95% classification pass rate on the held-out set; two weeks of shadow running with zero external actions; local versus cloud decided per task |
| 2. Supervised with training | Preconditions: kill switch, sender verification, a tested backup. Then corrections capture (draft, edit, reason, severity), rule suggestions, regression runs, and the metrics report | Owner corrects an error, approves the rule, and the pass rate rises with no older case regressing; five hours a week saved for four consecutive weeks |
| 3. Production safe | Hash-chained audit; full injection suite; restore runbook | Kill the worker mid-task 20 times: no lost task, no duplicate external action; restore from backup onto a clean machine; all injection cases blocked |
| 4. Appliance | Installer, updates, onboarding | A second business goes live on a fresh Mac mini in under one day with no custom code |
| 5. Workforce | Unfreeze SPEC M7 to M9; switch on the estimator and bookkeeper at Hive, each through its own Stage 1 and 2 gates | A sales employee hands an approved job to an operations employee with no duplicate record and no permission exceeded |

**Frozen until the Stage 2 gate passes:** SPEC milestones M7 (scheduling, procurement, portal), M8 (planner, learning, MCP gateway) and M9 (export and import). The one exception is the part of M8's learning loop that Stage 2 needs: corrections becoming suggestions.

**Only the owner can do:** the Stage 0 labelling and timing, real material for `apps/hive/memory/`, Microsoft 365 credentials, and the open decisions in `docs/BUILD_LOG.md`.

## Commercial model and open risks

Sell one offer first: an installed appliance plus a monthly managed fee. The employee library becomes a second product only after two or three businesses run the same template.

The durable value is the training engine, the permission and approval engine, the quality engine and the Business HQ experience. The Mac mini, the models and the frameworks stay replaceable.

The same Office Administrator adapts to a plumber, electrician or property manager by swapping knowledge, permitted tools, workflows and test cases. That reuse only counts once Stage 4 proves a second install needs no custom engineering.

Open questions to answer before pricing:

- [ ] Is Hive Property Services the same business as the fit-out HIVE in `apps/hive`?
- [ ] How many enquiries does Hive get in a normal week, and is five hours reachable from enquiry handling alone?
- [ ] What does one appliance cost per month to run and support, including your own hours?
- [ ] Who is liable when an approved action is wrong, and what do the customer terms say?
- [ ] What do the Privacy Act 2020 obligations require for customer data held on the box and sent to a cloud model?
- [ ] What verification do Google and Microsoft require before an app can read customer mailboxes at scale?
- [ ] Does "AI employee" set expectations the product cannot meet? Test "assistant" wording with the first customer.
- [ ] What is the recovery time if the Mac mini fails or is stolen, and is that acceptable to a trade business?
- [ ] Which hardware specification runs the chosen local model at acceptable speed?
