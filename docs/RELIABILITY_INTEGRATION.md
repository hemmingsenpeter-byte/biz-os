# Reliability integration, 12 November 2026

This integrates `claude/zen-edison-rurxg7` (`b47f688`) and
`claude/nifty-lamport-ixolby` (`474c3c2`). The retry/failure alerts, opt-outs,
budget fixes and tax/payment corrections remain alongside the owner controls,
correction history, scorecard and receptionist fixtures.

## Pause acknowledgement

Pause and resume transitions serialize on a PostgreSQL advisory transaction lock.
Their timestamps increase strictly, including within one millisecond or when the
clock moves backwards. State uses that ordering rather than random UUID order.

External and money tools, composites, and approval mutations obtain a shared
dispatch lock after blocking preflight and immediately before execution. A pause
response waits for previously admitted transactions to finish. Once it commits,
queued actions cannot begin dispatch until resume. An already admitted provider
call may finish while the pause request waits; existing idempotency and uncertain
send reconciliation still apply.

API transactions commit before the HTTP response is sent. A failed commit cannot
return a successful acknowledgement of a pause, resume or edit.

Dispatch uses a nonblocking lock attempt so budget/operation locks cannot form a
cycle with a waiting transition. A refused workflow attempt rolls back its entire
transaction before parking, then retries after resume. Inbound collection and
read-only access continue. Staff may pause; only the owner may resume.

## Versioned edits and measurements

`POST /v1/approvals/{id}/edit` now requires the integer `version` the editor loaded,
alongside `inputs`, `reason` and `severity`. Missing versions return 422; stale
versions return 409 without changing inputs or correction history. The console
preserves unsaved corrections and asks the editor to review the latest proposal
before trying again. Approval remains a separate version-bound action. Deploy the
updated API and console together; older open clients must refresh.

Approval without edit is unchanged approvals divided by **all proposals**, including
rejected and pending proposals. Error-free rate describes approved operations.
Missing or incomplete legacy correction history is unknown, unless a recorded
error already establishes an error. The quality rate stays unmeasured when an
unknown approval exists. Time savings remain unmeasured without a valid timed
baseline. Serious errors remain unmeasured until reversals are captured;
recorded autonomy demotion events are disclosed separately.

## Evaluation contract

The primary score is complete cases passed / cases attempted. Mean partial-check
score is diagnostic only. Every check, including an exact finite confidence
minimum, must pass for the case to pass. Expected labels default to exact checks
when checks are omitted. Schema-only checks cannot establish a technical gate.
Missing/empty datasets, unavailable requested tiers, and invalid configuration
fail; a requested cloud comparison cannot silently use the local backend.

Reports include dataset and prompt/schema SHA-256 hashes, split/provenance,
review status, actual tier, backend class and configured model. Use:

```sh
biz eval receptionist.triage --tier shadow \
  --cases roles/receptionist/tests/eval/triage_heldout \
  --json --output /path/to/private-eval.json --gate
```

Output files have mode 0600 because case results can contain customer material.
The technical gate requires at least 50 held-out cases, at least 95% passing cases
(or a stricter configured rate), and outcome checks. Shadow additionally requires local quality
within the configured cloud margin. `--gate` also requires reviewed historical or
mixed provenance and an identified model. These flags assess the declared dataset;
they do not prove business volume, time savings, production supervision or sender
verification. No autonomy or routing is changed by evaluation.

Model/schema invocation errors invalidate a shadow comparison. A cloud service
failure cannot make the local model appear ready by lowering the cloud score.
Schema-valid but incorrect cloud answers remain scored comparison evidence.

All 108 existing receptionist fixtures remain synthetic and owner-unreviewed.
Triage has 30 tuning and 57 held-out cases; reply handling has 21 tuning cases.
Fake oracle passes validate fixture wiring, not model accuracy. CI checks production
prompts, schemas, serialized inputs, rule routing, offered-slot indices and runtime
confidence thresholds. Real model runs and owner-labelled history remain pending.

## Migration reconciliation

The branches independently used revision `0020`. The canonical lineage is now
`0019 -> 0020` (controls/corrections) `-> 0021` (reconciliation and tax).
Only one `0020` file is shipped. Existing tax-only databases stamped `0020` are
recognized by schema inspection; no manual stamping or historical tax recalculation
is needed. Fresh, `0019`, control-only `0020`, tax-only `0020`, and already-combined
`0020` layouts all reach the same union. Existing invoice/bill amounts, taxes,
payment state and control/correction records are preserved.

Startup migrations serialize before inspecting the version/schema. Reconciliation
validates column definitions and append-only triggers and fails transactionally on
incompatible layouts. Back up before applying migrations and investigate any
incompatibility rather than stamping past it.

Downgrading `0021` means canonical **control-only** `0020` and drops both tax
columns. It is refused by default. The programmatic helper accepts the explicit
boolean `allow_data_loss=True` for disposable databases or a deliberate destructive
rollback. For production recovery, restore the verified backup. Downgrade does not
reconstruct which historical `0020` branch originally created a database.

M7–M9 remain frozen under `PLAN.md`. Sender verification, owner timing/volume,
historical labels, held-out reply/booking coverage and actual model/shadow runs are
still prerequisites for the relevant business and live-sending gates.
The existing HIVE composition still lists estimator and bookkeeper alongside the
receptionist; align pilot role activation with `PLAN.md` before live use.
