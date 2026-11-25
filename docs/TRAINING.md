# Correction training proposals

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

This slice captures provenance and creates a reviewable rule/case proposal. It
does not run a candidate comparison, publish a prompt/SOP version or activate a
rule. Those require a separate versioned comparison and activation workflow,
with no older-case regression and the owner's explicit approval. The live HIVE
pilot gates in [PLAN.md](PLAN.md) remain in force.

Migration `0024` adds these records and pins SOP definitions for recovery. Follow
the [upgrade and drain procedure](../deploy/README.md#upgrading-sop-definitions)
before changing a running installation.
