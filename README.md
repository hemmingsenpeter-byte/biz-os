# BIZ OS

A modular platform for AI-run small businesses. The platform has a business-agnostic **kernel**, **capabilities** (skills with code), **ports** (adapters to outside systems), **role packs** (roles as data) and **industry packs** (one trade's vocabulary). A business is a thin **composition** under `apps/`.

The full spec is [docs/SPEC.md](docs/SPEC.md), and the working rules are in [CLAUDE.md](CLAUDE.md).

## Quick start (dev)

```sh
uv sync
uv run pytest -p no:cacheprovider     # real Postgres 16 + pgvector via pgserver; no Docker needed
uv run biz load apps/demo             # print the loaded manifest
uv run biz doctor apps/hive           # validate packs, ports, schedules, budgets
```

Tests start a throwaway Postgres 16 with pgvector through the `pgserver` wheel. They never use the live network: every port has a fake in `ports/fakes`.

## Status

| Milestone | State |
| --- | --- |
| M0 Kernel and demo app | Built. Acceptance passes in `apps/demo` and `apps/hive`. Mac mini deploy not yet run (see `deploy/README.md`) |
| M1 Models, memory, evaluation | In progress |

## How a role runs

1. A packet is published, for example `service_request.received`. The bus validates it against `packets/<type>/v<n>.json`.
2. Every role that consumes that packet type gets a wake-up. Wake-ups for the same role coalesce.
3. On the heartbeat (`Runtime.tick`), the role's budget is checked. Each triggered SOP then starts as a DBOS workflow.
4. SOP steps are `tool`, `draft`, `approve` and `publish`. Each step checkpoints in DBOS and in the `sop_steps` ledger, so a killed worker resumes without repeating work.
5. Any tool with an `external` or `money` side effect goes through `policy.check`. Below autonomy level 3 it is staged, and the run waits. Approving it with `workflows.gate.decide` runs exactly the approved inputs, once.
6. The task is marked done only when its declared evidence, such as `approval.granted` and `packet:message.sent`, is in the activity log.

## Dev-box notes

- This Intel macOS 12 machine can't build some new native wheels, so `pyproject.toml` constrains `cryptography<48` and `cbor2<6`.
- zsh doesn't word-split `$VARS`, so pass multiple paths to commands literally.
