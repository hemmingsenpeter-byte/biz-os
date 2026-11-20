# Private receptionist evaluation

The owner supplies the source enquiries and confirms the reference category for each
one. The CLI prepares the cases on the local machine and keeps the historical tuning
and held-out sets in separate directories. Use the packaged synthetic cases to review
routine and adversarial examples alongside the historical sample.

## Collect and label historical enquiries

1. Choose at least 100 distinct conversations that reflect typical inbox traffic.
   Export one suitable message per conversation to a UTF-8 CSV with these columns.

   ```csv
   thread_key,channel,subject,body
   ```

   Use a stable opaque `thread_key` for each conversation. Give forwards and repeats
   from the same conversation the same key. Channels are `mail`, `form` and `sms`. Strip names,
   signatures, street addresses, account IDs and sensitive
   business details before saving the CSV. Keep enough context to classify the
   enquiry. The importer additionally masks email addresses, phone-like strings
   and web links. Inspect the review sheets for identifying details missed by the
   automatic pass. These masks only cover recognizable patterns.
2. Keep the source, workspace and reports outside Git. Use a private directory on
   an encrypted volume the owner controls. The CLI creates mode 0700 directories
   and mode 0600 outputs. Reuse the same workspace and include the complete sample
   when creating a new version. Preserve `splits.json` in the private backup so
   repeat and revised cases keep their original assignment.

   ```sh
   uv run biz eval-data prepare "$BIZ_EVAL_SOURCE" "$BIZ_EVAL_WORKSPACE" v1
   ```

   This writes blank category columns under `v1/tuning/review.csv` and
   `v1/heldout/review.csv`. The import deduplicates by thread and normalized
   message text. Keep the held-out sheet and per-case score reports with the
   evaluator or owner. Use only the tuning sheet and tuning failures to adjust
   prompts. Add a fresh owner-labelled cohort when repeated use of the same
   held-out set starts to inform tuning decisions.
3. The owner fills in only the `category` column, using `enquiry`, `service_request`,
   `supplier`, `spam` or `other`. Save as UTF-8 CSV. Use a new version if text or
   category evidence needs later correction. After reviewing every case, the owner
   confirms the labels using an opaque reviewer ID.

   ```sh
   uv run biz eval-data confirm "$BIZ_EVAL_WORKSPACE" v1 --owner owner-01
   ```

   The command seals the version under `v1/confirmed/`. Each split includes the
   prompt and schema and a dated local owner attestation with the dataset and
   source digests. Keep the confirmed files at their original paths in the
   workspace. Evaluation checks case membership against the split ledger and
   source cohort. Restrict workspace access to the owner and the evaluator
   designated by the owner.

To give the owner a reviewable copy of the packaged synthetic sets, use one
`review-sheet` export per split. Inspect the `message` and `expected` columns and use
`owner_notes` for corrections. These exports preserve the packaged provenance and
the fixture quality checks.

```sh
uv run biz eval-data review-sheet roles/receptionist/tests/eval/triage_heldout \
  "$BIZ_EVAL_WORKSPACE/synthetic-triage-heldout.csv"
uv run biz eval-data review-sheet roles/receptionist/tests/eval/handle_reply_heldout \
  "$BIZ_EVAL_WORKSPACE/synthetic-replies-heldout.csv"
```

## Capture the owner baseline

Time at least 20 real owner tasks with a stopwatch and record elapsed seconds.
Use distinct opaque IDs. Record the approval-review seconds when observed and
leave the field blank when it was unmeasured. Count enquiries separately over a
normal complete week. Keep the measurement CSV outside Git.

```csv
sample_id,owner_seconds,approval_seconds
```

```sh
uv run biz eval-data baseline "$BIZ_BASELINE_CSV" "$BIZ_EVAL_WORKSPACE/baseline.json" \
  --week-start "$BIZ_WEEK_START" --week-end "$BIZ_WEEK_END" \
  --weekly-enquiries "$BIZ_WEEKLY_ENQUIRIES"
```

Dates use YYYY-MM-DD. The start date is inclusive and the end date is exclusive.
The report estimates weekly owner workload from observed mean task duration and
enquiry count. An unmeasured review duration appears as null.

## Score and compare real models

Use the same confirmed cases for both tiers. Identify the local model revision
using its installed Ollama digest and the cloud model using the exact provider
revision selected for the run. Set `BIZ_OLLAMA_MODEL` and, for authorised cloud
evaluation, `BIZ_CLAUDE_MODEL` and `ANTHROPIC_API_KEY` as for the existing model
CLI. Check that the version evidence corresponds to the configured model before
recording it. Private runs that include the local tier require a loopback
`BIZ_OLLAMA_URL` and an installed noncloud model. The evaluator connects directly
to the loopback address without using inherited HTTP proxies.

```sh
uv run biz eval receptionist.triage --tier local \
  --cases "$BIZ_EVAL_WORKSPACE/v1/confirmed/heldout" \
  --local-model-version "$BIZ_LOCAL_MODEL_VERSION" \
  --output "$BIZ_EVAL_WORKSPACE/local-v1.json" --gate
```

To compare tiers in one controlled run, use `--tier shadow` with
`--cloud-model-version "$BIZ_CLOUD_MODEL_VERSION"` and `--allow-cloud-data` after
the owner approves sharing that redacted cohort with the configured provider.
Use `--tier cloud` for a cloud-only report. Private runs write a private file and
print aggregate results. Keep prompts and reports off Git and restrict access to
the held-out details. A model response or failure can reproduce content from the
message.

Reports include dataset and prompt/schema hashes, dataset version, actual model
tier, model ID, recorded revision, adapter version, and per-case latency, token
usage and model API cost when reported by the adapter. Local model API cost
excludes hardware and electricity. Cloud cost is derived from the adapter price
table. Cases without returned usage are counted separately. The report includes
those cases in the score and duration totals.

The technical gate requires at least 95% fully correct classifications over at
least 50 held-out cases with outcome checks. Newly confirmed historical cases
require valid structured output and the exact owner-labelled category. When the
label is anything other than `other`, the output must also meet the intake
boundary of 0.6. For example, `enquiry` at 0.59 is routed as `other` in production
and fails that case. An exact `other` at 0.25 remains `other` and passes, while
`spam` at 0.25 still fails an `other` case because the model chose the wrong category.
Malformed or missing confidence fails the output schema. Packet routing and
escalation require separate workflow evidence.

Existing confirmed datasets still use their sealed scoring checks. Use a new
owner-confirmed dataset version for the revised rubric and record the version in
comparisons.

The evaluation deployment flag also
requires owner-confirmed historical evidence and a configured real adapter and
recorded model revision. A local-versus-cloud recommendation requires a valid
comparison with a real cloud adapter and recorded cloud model revision. The model
gate is one input to the pilot decision in the build plan.

Offline tests exercise these paths with fake ports and synthetic data.
