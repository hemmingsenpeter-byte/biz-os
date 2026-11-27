# Correction training and local comparisons

Training starts with an owner's correction to an approval. The Training workspace
lets the owner propose a rule, select the original model draft and supply the
expected corrected model output. A review accepts that proposal for testing only.
It changes no deployed prompt, SOP, policy, autonomy level or external action.

## Create and review a proposal

1. Open **Training** as the owner and choose a correction. Review its before/after
   inputs, reason, severity and version. Staff cannot access training evidence.
2. Select the relevant captured draft from that operation's workflow. The snapshot
   includes the exact prompt, output schema, untrusted input, observed output and
   available model metadata. Unknown model revisions remain unknown.
3. Write a proposed rule and enter the expected model output as JSON. Tool inputs
   and model outputs can differ, so the system does not guess how an approval edit
   should change the model's output. The expected object must satisfy the captured
   schema, including its declared JSON Schema dialect.
4. Create the proposal. An identical repeat returns the same record; a different
   payload for that correction conflicts with the existing immutable proposal.
5. Inspect the linked regression-case preview. Accept it for testing or reject it.
   Acceptance is blocked while paused; rejection remains available. Reviews are
   immutable and an incompatible repeat returns a conflict.

Legacy corrections without a captured draft remain visibly incomplete. They can
be documented and rejected, but cannot be accepted as source-backed test cases.
The system never reconstructs missing historical prompts or model revisions.

## Revise a candidate

The original proposal and its review remain immutable. Create a successor version
with the full proposed system prompt, rule, expected model output and checks. A
version records its parent, source snapshot and content hashes. Concurrent edits
against an older parent conflict; the console preserves the unsaved text so the
owner can reconcile it against the latest version.

Each version needs its own acceptance for testing. Acceptance of a root proposal
or an earlier version never authorizes a new version. Rejected versions remain
visible in history and can have a successor. A successor still needs genuine
captured source evidence; supplying new text cannot repair missing provenance.

## Freeze a private tuning cohort

Select accepted versions for one task and output schema. Freeze the exact source
inputs, expected outputs/checks and baseline prompts into a cohort, then review its
training-purpose attestation. This is private tuning authorization, separate from
the historical-data and held-out readiness attestations. No held-out dataset is
automatically imported, relabelled or made available for tuning.

The comparison applies the target's captured baseline prompt and its proposed
prompt freshly to every cohort case. The frozen setup identifies both prompts;
the captured baseline is not assumed to be the current deployed prompt. It reports the
target correction and each older case separately. A baseline pass becoming a
candidate failure is a regression even if the average score improves. The historical
captured output is reference evidence; both baseline and candidate are inferred
fresh for the actual comparison.

## Run and inspect a comparison

The owner freezes the local model name, installed digest, Ollama and adapter
versions, generation settings, seed, timeout and repetition count with the request.
The API queues a durable job and returns promptly; the console polls progress and
results. A host-side comparison executor processes queued jobs against literal
loopback Ollama. Arbitrary private-network endpoints, cloud-backed models and
inherited HTTP proxies are refused. Execution inside Compose is outside this slice.

Run the executor on the appliance host with the installation's application and
DBOS database URLs configured. Migrate the release first; the comparison worker
does not start the business runtime or migrate its schema:

```sh
uv run biz training comparison-worker
# Or run/resume one job whose UUID is shown in the console:
uv run biz training run-comparison <job-uuid>
```

Use the same frozen release and locked dependencies for the API and host worker.
Drain queued comparisons before changing the Python environment or dependencies.

Use the installed `/api/tags` digest and `/api/version` from loopback Ollama for
the queue form. The adapter version is the installed `biz-port-model-ollama`
package version. A mismatch is refused before private inference; the executor
also checks the pinned implementations and model metadata after inference.

Each case/side is checkpointed, including a durable start before inference. Completed
work is reused on duplicate requests and replay. An interrupted attempt whose result
was not saved remains unknown; it cannot become a clean success through a silent
retry. Model drift, missing results and inference failures invalidate a comparison.
Review per-case output, checks, latency and metering evidence. Local API cost is
reported separately from hardware and electricity, which are not measured.

A valid measurement can still have a regression verdict. That verdict blocks
acceptance of the proposed change regardless of any aggregate score improvement.

Comparison results establish no deployment readiness or time savings. They change
no prompt file, SOP, policy, permission or autonomy level and provide no activation
action. Production activation and rollback remain a separate release workflow.

## Evidence and privacy

New successful draft steps store their snapshot in the same transaction as the
workflow step ledger. Replay reuses the observed evidence. Snapshot selection is
limited to the corrected operation's actor and workflow, before the correction
occurred. Stored snapshot and candidate integrity hashes are checked before they
are used; changed evidence is refused.

Snapshots, proposals and reviews are append-only database records with row and
TRUNCATE guards. Owner-only API responses disable caching. Audit records carry
identifiers and hashes rather than copying private draft text into diagnostics.

Case previews are **private tuning evidence**. Testing review is different from a
held-out historical-data attestation. Every preview keeps `owner_reviewed: false`
and `readiness_established: false`; it does not establish model or business gates.
The evaluator respects private flags on individual case files, and refuses an
unconfirmed standalone private case before inference. The existing private-data
authorization and confirmation workflow still governs model evaluation.

## Current scope

The comparison sprint covers one task kind and schema, a frozen tuning cohort,
one pinned local backend and a minimal owner results view. It does not publish a
prompt/SOP version or activate a rule. The live HIVE pilot gates in
[PLAN.md](PLAN.md) remain in force.

Migration `0024` adds source proposals and pins SOP definitions for recovery;
`0025` adds versioned comparison records. Follow
the [upgrade and drain procedure](../deploy/README.md#upgrading-sop-definitions)
before changing a running installation.
