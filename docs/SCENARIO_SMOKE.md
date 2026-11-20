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
attempt. Exit code 1 indicates a failed check or execution error. Exit code
2 indicates an unavailable or unverified model.

The runner checks model category, effective routing, complaint escalation,
duplicate intake, draft recipient, dispatch suppression and the fake provider
invocation counters. A passing offer check confirms suppression and the intended
recipient. Review the draft for accurate slots and business facts separately.
The JSON contains fictional workflow text for that review and records model
latency and tokens where available. Other catalogue cases remain outside this
first slice. Historical owner-reviewed evaluation uses its separate workflow.
[Private receptionist evaluation PR](https://github.com/hemmingsenpeter-byte/biz-os/pull/8)

The offline CI command exercises the lead case with fixture inference.

```sh
uv run python -m deploy.scenario_smoke --fixture-only --repetitions 1 \
  --out /tmp/biz-fixture-smoke.json
```
