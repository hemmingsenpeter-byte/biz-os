# Receptionist scenario smoke

Run the four version 1 fictional cases with the HIVE receptionist SOPs. The
command creates fresh PostgreSQL application and DBOS databases for every
attempt and destroys them after the attempt. Form input goes through the API.
Mail goes through the fake inbox and real poller. All business adapters are
fake and the gateway is forced into shadow mode.

Start a trusted local Ollama server on `127.0.0.1:11434` with cloud routing
disabled and a noncloud model installed. Set `BIZ_SMOKE_MODEL` to its installed
tag and `BIZ_SMOKE_DIGEST` to the corresponding manifest digest from
`/api/tags`. Keep the report outside the repository.

```sh
uv sync --locked
uv run python -m deploy.scenario_smoke --model "$BIZ_SMOKE_MODEL" \
  --digest "$BIZ_SMOKE_DIGEST" --out "$BIZ_SMOKE_REPORT"
```

Use `--case R01` to run only the lead-to-meeting case, `--dry-run` to list
selected cases, and `--repetitions` and `--timeout` to configure the run.
The default repeats each case three times. The report is written after each
attempt. Exit code 1 indicates a failed check, execution error or required manual
review. Exit code 2 indicates an unavailable or unverified model. An interrupted run
returns 130.

The runner checks model category, effective routing, complaint escalation,
duplicate intake, draft recipient, dispatch suppression and fake provider invocation
counters. Meeting offers are now rendered by `booking.render_offer` from validated
calendar slots, with explicit time-zone and UTC-offset labels; the model does not
write the offered dates or body. The runner compares the canonical body and stored
reply-slot metadata with the calendar step and records offered-slot accuracy as an
automated check.

Every service request now publishes an owner escalation and leaves its task open.
R04 verifies that the escalation packet is linked to the triggering enquiry and its
task activity; routing it as a service request alone does not pass. This requests
owner review of earlier work and applicable terms and promises no warranty remedy.
Workflow and readiness checks remain separate, and either type of failed check makes
the run fail in both fixture and actual-model modes.
The JSON contains fictional workflow text for that review and records model
latency and tokens where available. Other catalogue cases remain outside this
first slice. Historical owner-reviewed evaluation uses its separate workflow.
[Private receptionist evaluation guide](RECEPTIONIST_EVAL.md)

The runner writes its planned cases and unverified attempts before starting. Every
report says that workflow smoke does not establish readiness. A timed-out or
interrupted attempt keeps its invocation counters provisional, and the runner stops
without treating the remaining attempts as verified. The report lists those attempts
under `unverified_attempts`. Fixture inference proves workflow wiring only; it does
not prove model quality or the held-out historical gate.

The offline CI command exercises all four cases with fixture inference. Use
`--case R01` to select a single fixture case.

```sh
uv run python -m deploy.scenario_smoke --fixture-only --repetitions 1 \
  --out /tmp/biz-fixture-smoke.json
```
