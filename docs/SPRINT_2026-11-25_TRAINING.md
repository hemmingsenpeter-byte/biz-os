# Correction training and receptionist pilot sprint

This sprint builds on draft [PR #15](https://github.com/hemmingsenpeter-byte/biz-os/pull/15)
at `bdf10e6`. Its goal is a usable first correction-training slice and closure of
the receptionist's two remaining workflow-scenario gaps.

## Coding scope

- Capture immutable draft provenance alongside the workflow ledger. Preserve
  unknown model revisions and incomplete legacy corrections honestly.
- Add owner-only correction proposals, explicit source selection, schema-validated
  expected outputs and testing-only acceptance/rejection in the API and console.
- Protect private case previews in the evaluation loader and validate frozen
  evidence hashes, source relationships and timestamps.
- Escalate service requests to the owner, preserve the open task and publish no
  warranty promise or customer action.
- Render meeting offers deterministically from validated calendar slots with
  explicit timezone labels. Preserve recipients, approval and shadow controls.
- Pin SOP definitions and prompts; refuse incompatible unfinished recovery before
  business steps. Migration `0024` refuses legacy unfinished work before changing
  the schema, so the original release can still drain it.

## Acceptance

Real PostgreSQL tests cover immutable records, concurrent duplicate requests,
conflicting submissions, altered evidence, source selection, schema dialects,
owner/staff permissions and pause boundaries. Browser acceptance covers the full
proposal/review flow and verifies that it changes no deployed instruction or send.

Both demo and HIVE receptionist tests must show one linked service escalation,
an open owner task, replay deduplication and canonical appointment text. All four
scenario fixtures must pass their workflow and automated readiness checks while
continuing to state that they do not establish pilot readiness.

Release checks include the complete Python suite, Ruff, mypy, both composition
doctors, web lint/typecheck/build, real-backend Chromium acceptance and CI's
packaged Docker and encrypted recovery jobs. Independent reviews must clear
proven defects before the branch is submitted.

## Boundaries and next work

Local completion checks passed: **1138 Python tests**, Ruff lint/format, mypy,
both composition doctors, frontend lint/typecheck/build and real-backend browser
acceptance. Independent reviews cleared the source-provenance, privacy, schema and
cross-version recovery findings. The stacked PR's packaged and recovery CI checks
remain required before merge.

HIVE remains shadow-only. No live model, mailbox or appliance acceptance is implied
by fixture tests. Owner-labelled history, timing/volume measurements and two weeks
of actual shadow evidence still gate a supervised pilot.

The training comparison, candidate versioning, activation and rollback loop is
the next feature slice. Production outcome/reversal capture and operator alert
delivery also remain future work. M7–M9 remain frozen under PLAN.md.

See [TRAINING.md](TRAINING.md) for the implemented owner workflow and evidence
semantics, and the deployment guide for the required drain before upgrade.
