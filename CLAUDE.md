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
