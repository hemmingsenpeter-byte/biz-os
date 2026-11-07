# Build log

## 2026-11-07 (evening): owner's open decisions, then M6 money (invoicing, payables, cash)

The owner asked for these decisions to be made for them. Each one is below, with the reason.

### Decisions made (check these)

- **Escalation signals merged** (`49d17b2`). The `escalation.raised` packet schema already existed, so the only schema addition is the optional `signals:` key in `capability.yaml`. The "declared but not yet enforced" warning is gone.
- **Which `escalate_when` conditions a SOP is checked against** (`5e464bc`). The old skip turned off every role condition for any SOP with its own stop step, so a low-confidence pricing draft went through unchecked. Removing the skip outright broke `handle_reply`, which reads the draft's confidence itself and finishes with its own `reply.handled`. The rule now: `confidence_below` is skipped only for a draft whose confidence a later step reads; signals are skipped only in a SOP with its own escalation step (the estimator's pricing SOPs already escalate on margin themselves). Pricing drafts are checked for confidence again (new test).
- **Autonomy: policy.yaml is an upper bound** (`00d6fb4`). For a key named in `policy.yaml`, the effective level is the lower of the stored level and the configured one. Lowering the file always takes effect; demotions still stick; a promotion or owner-set level can't go above the file. Keys the file doesn't name still use the stored level.
- **#2 daily running totals built** (`901f5df`). New `limits.max_daily` (keyed like `max_amount`; a tool or decision-type key counts only that key's spend, `default` counts all) and `limits.day_timezone` (default UTC; HIVE set to `Pacific/Auckland`). The total is **business-wide**, not per role, because it guards what leaves the business in a day. A ledger (`money_spend`, migration `0019`) records every money tool call that runs; the check and the insert share an advisory lock held to commit. Over the total needs approval, or is denied when `over_limit: deny`. Demo has `max_daily: 2000`; HIVE's is commented out for the owner to set. `biz doctor` now checks money limits and the time zone.
- **mypy** (`f8fce1d`). `explicit_package_bases` plus an exclude list fixes the duplicate `conftest`; with that, only 14 test typing errors were left (not 66), and they are fixed. `uv run mypy .` is clean and CI now runs it.
- **The confidence field on the receptionist's drafts stays required.** `confidence_below` fails closed on a missing value anyway, so requiring it only makes the failure visible earlier.
- **Infinite and text money limits stay refused**, and the 2-not-3 cloud calls test stays: both are the fail-closed behaviour.
- **Worktrees:** all four removed (the signals branch is merged). About 1.3 GB freed.
- **M6 started, built on fakes, nothing live.**
  - **Xero adapter uses Xero's REST API over httpx, not xero-python** — this departs from CLAUDE.md's reuse list, like M365 did with msgraph-sdk. xero-python is synchronous (urllib3), our ports are async, and httpx is mocked the same way as every other port. The dependency is removed. Invoices and bills are created as **DRAFT only**, with a durable ledger (`port_xero_posts`, migration `0014`) plus Xero's `Idempotency-Key`. A create that may have reached Xero is reconciled by reference before any retry; a "sending" row under 2 minutes old is treated as in flight. It uses a Xero custom connection (client credentials); see `deploy/.env.example`.
  - **Invoicing:** payment stages per job (share of value or fixed); one invoice per reached milestone, never past the job value; due date from `invoicing.payment_terms_days` (default 20). `invoice_post` is **external, not money**: it creates a receivable draft and moves no money, so HIVE's `max_amount: 0, deny` doesn't block it. The owner still approves each one (`invoice_posting: 2`), and the posted amount must equal the stored total. A poller reconciles payments (35-day look-back, deduped by external id) into `payment.received` and marks invoices paid.
  - **Payables:** bills match a PO by number, or by supplier and total when there is no number, within `payables.match_tolerance_pct` (0 in both apps); a mismatch escalates. Matched bills post to Xero as drafts after approval.
  - **Bank details change only by verified call-back.** `bank_change_request` records the change as pending, with the phone number from our own records, never one from the request. `bank_change_confirm` applies it only when the number called matches. It is a money action with no amount, so policy sends it to the owner at every autonomy level, and an invariant test bars every role pack from listing it. Payment runs include only suppliers with verified bank details.
  - **Cash:** `cash.forecast` is pure arithmetic over what it is given: 13 Monday-started weeks, overdue items in week 0, a `later` total, other currencies listed rather than converted, and `cash_below_floor` when the floor is breached. `invoicing.receivables` and `payables.due` are new read tools that feed it, so no capability reads another's tables.
  - **Bookkeeper role** (both apps): `milestone_invoicing` and `bill_matching`. It may not touch bank changes.
  - Migrations `0014`-`0016` were empty stubs that were never deployed, so they were filled in place rather than adding new revisions.
- **Checks:** full `pytest` green, `ruff` clean, `uv run mypy .` clean, `biz doctor` passes for demo and HIVE. Nothing pushed.

### Waiting on the owner

- **HIVE's Xero:** create a custom connection and set `BIZ_XERO_*`. Confirm the sales (200) and purchases (300) account codes. Tax uses each account's default; `LineItem.tax_rate` is not sent.
- **HIVE `limits.max_daily`** (commented out) and a cash floor.
- **M6 acceptance, not done yet:**
  - Debtor sequences (reminders) need a new packet or trigger for overdue invoices, and they send mail. Not built.
  - The cash forecast does not reconcile with Xero yet: the opening bank balance needs a new `AccountingPort` method (a port interface change). For now the opening balance is passed in.
  - No SOP runs the forecast yet (no trigger packet; `brief.daily` is M8).
  - POs reach payables only through `payables.po_record`: `po.issued` has no PO number, so procurement (M7) should call the tool or the schema needs a `number`.
  - No owner screen or route stages `bank_change_confirm` yet. The tool and its policy gate are ready.
- **Still open from before:** `autonomy_defaults` in role.yaml is never read. A raised budget that still can't cover the next call wakes and stops on every heartbeat.

## 2026-11-07: kernel review fixes (before M6 money code)

### Decisions made (check these)

- **Bad money values are denied** (#1): an amount or a configured limit that is negative, NaN, ±inf, a string or a bool is a `Deny`, not an approval. That covers an infinite limit and a string limit like `"500"`. A draft tool that wraps a money tool used to come back as Allow; it now takes the strictest inner gate.
- **Budgets can't overshoot** (#3): each charge takes a per-role advisory lock and checks the new amount. The registry runs a cost check before a paid tool, charges up front and refunds on failure. Before a cloud call, the router estimates the cost from that task's priciest call this month. Free local calls are no longer recorded. `test_router_budget` now expects 2 cloud calls, not 3.
- **Budget-stopped runs** (#6) get the new run status `budget_stopped` instead of hanging in `running`. Their wake-up stays pending, and they resume once the limit is raised. `BudgetExceeded` now survives pickling, which DBOS needs.
- **Approver edits keep the idempotency key** (#4). Every new version keeps the original key, and an edit still sends the run back to waiting.
- **Approval links** (additive): approvals carry their run, task and job IDs. New read routes: `GET /v1/approvals/{id}` and `GET /v1/tasks/{id}`. No packet schema changed.
- **Mail in production** (#8a): `api/main.py` now builds the runtime first and passes its mail port to `create_app`. Before this, magic-link login returned 503 in production.
- **Evidence rules merge to the strictest** (#8b): rules are combined, not overwritten. Conflicting declarations fail `biz load` and `biz doctor`.
- **Configured autonomy levels survive** (#7): the first approval used to write a level-1 row that overrode `policy.yaml`, which would have pinned a configured level 3 or 4 down to 1 (latent for HIVE). `autonomy_levels.level` is now nullable (migration `0018`).
- **`escalate_when` is enforced** (#5, `d51d863`): `confidence_below: N` is checked on draft outputs, and missing or bad confidence escalates. A match ends the run as escalated with the task left open, through the existing stop-step path. The loader and doctor reject unknown conditions. A role with `confidence_below` must require `confidence` in every draft schema, so `book_meeting` and `handle_request` gained it. Signal conditions (the estimator's `margin_below_policy` and `unknown_trade_item`) load with a "declared but not yet enforced" warning.
- **Test Postgres is deleted at exit** (`712eea0`): each `pytest` run had left a `bizpg-*` folder of 100-900 MB in TMPDIR. 258 of them (36 GB) filled the disk.
- **Checks on the merge:** `pytest` and `ruff` pass, `mypy kernel/biz_kernel capabilities ports` is clean, and `biz doctor` passes for both apps.

### Waiting on the owner

- **#2: daily running totals for money limits.** A new policy concept, not built.
- **Escalation signals** (branch `worktree-agent-ad3448cd501d446e5`, commit `49d17b2`, not merged): adds a `signals:` key in `capability.yaml`, quoting emitting signals, and a new `escalation.raised` packet that the kernel publishes. These are schema additions, so they need approval. Even once merged, the hook skips SOPs that already have a stop step or publish `escalation.raised`, which includes the estimator's. The estimator signals would still not fire until that is resolved.
- **Stored vs configured autonomy:** an owner-set or promoted level in the DB still beats a later `policy.yaml` edit for the same key.
- **`autonomy_defaults` in role.yaml** is never read anywhere.
- **A raised budget that still can't cover the next call:** the run wakes and stops again on every heartbeat until it can.
- **`uv run mypy .` fails** on the duplicate module `conftest` (root and `kernel/tests`); it did before this session too. Checking `tests/` as well shows 66 errors in 18 test files that already existed. Fixing it needs a mypy config change.

## 2026-11-06 to 11-07: M4-M5 review fixes, cloud fixes, merge

### Decisions made (check these)

- **Quoting costs come only from the price book** (`fd480a9`). A cost the caller supplies for a price-book line is ignored with a `cost_override_ignored` warning. Price overrides still work, with a `price_override` warning, and the margin check covers them.
- **Negative values are refused** (`negative_value`): costs, prices, discounts and contingencies, and any discount of 100% or more. Negative quantities are refused on estimates but **allowed on variations**, where they price removed work as a credit.
- **Any unpriced line makes `margin_ok` false**, so it escalates to the owner. `estimate_check` recomputes from the stored lines, and `proposal_build` refuses a superseded estimate, a superseded scope, or a failed margin re-check.
- **Intake matches replies through a published comms tool**, `comms.match_reply` (read), instead of reading `cap_comms_outbox` (`cd65c60`). On the merge, the reply-token expiry rule from `922f0f0` moved into that tool, so an expired token matches nothing and the mail becomes a new enquiry.
- **Reply threads** (`922f0f0`): tokens expire, a thread books at most once, unverified senders are flagged, and customer text is fenced in approval cards.
- **Documents** (`ea79969`): the DocuSeal ledger is keyed on the idempotency key and webhooks match by submission id, so a re-request after a decline works; a signed copy is a separate document that references the signed version; template mode is opt-in (`BIZ_DOCUSEAL_ALLOW_TEMPLATE_MODE`).
- **The stopped agent's partial quoting work** is kept in `git stash` (`stash@{0}`) rather than discarded. `fd480a9` replaces it.
- **The reviewer's proof tests** (`review3/`) were run against the merge: the 5 quoting bypasses are all blocked, and the 2 documents defects are covered by `ea79969`'s own regression tests.
- **Checks on the merge** (`942a516`): 413 passed and 1 skipped (PDF, no Pango on the Mac); mypy, ruff and `biz doctor` for both apps are clean.

### Waiting on the owner

- **Defect #3, sender verification.** It is always false today. Fixing it means adding authentication results (`Authentication-Results`) to the mail port's inbound message, which is a port interface change.
- **`SignPort` idempotency key.** Whether to add an optional `idempotency_key` to the interface. The adapter works without it.

## 2026-11-06: first build session (M0 to M3)

### Decisions made during the build (check these)

- **Money fails closed.** A money action with no configured limit needs approval at every autonomy level. A `deny` limit can't be approved past.
- **Autonomy is the strictest of the two keys.** The level is the lower of the tool's own setting and the SOP's decision type. A decision type can never loosen a tool, and inner tools are judged by their own setting.
- **Approval messages are only hints.** The staged operation's row is the source of truth. A run acts only on an approval at the current version, and edits after approval send the run back to waiting.
- **Failed heartbeats keep their work.** A failed run re-queues its packet reasons rather than dropping them. A budget-stopped role keeps its wake-up pending until the limit is raised.
- **Evidence is enforced twice:** by an ORM hook, and by a Postgres trigger on `tasks` (migration `0004`).
- **Tokens are single-use.** Magic links and invites can't be replayed (`auth_used_tokens`, migration `0002`).
- **Gated tools must be idempotent.** `build_runtime` refuses any `external` or `money` tool without an `idempotency_key`.
- **No live network in tests.** The root `conftest.py` blocks every non-Unix-socket connection.
- **M365 uses Graph REST over httpx, not `msgraph-sdk`.** It is easier to mock and lighter. Bookings use Graph `transactionId` for idempotency.
- **New port contract: `CalendarPort`** (`free_slots`, `book`). No existing port interface was changed.
- **One webhook contract.** Ports parse `{"form", "headers", "url"}`. Twilio signatures are checked against `BIZ_TWILIO_WEBHOOK_URL`, the public URL, because the URL behind the proxy differs.
- **HIVE's composition drops the M0 `demo_clerk` placeholder** and runs the receptionist with real port names (`telephony_twilio`, `mail_m365`, `model_ollama`, `model_claude`).
- **Public routes only under `/webhooks/*`.** Telephony and form are at `/webhooks/telephony` and `/webhooks/form`; `/v1` stays private (Tailscale only).
- **Inbound contacts are rung 0 (unverified).** Enquiries never link to an existing party by a claimed email or phone.
- **Only a missed call gets the text-back,** at most once per number per 24 hours. Inbound SMS goes to triage.
- **The receptionist offers meeting slots but doesn't book yet.** Booking needs the customer's reply, which comes once comms reads replies.
- **The receptionist sends through comms.** Every reply passes the gate and opt-outs. The approver sees the exact to, subject and body, and `comms.send` refuses any mismatch.
- **Send ledgers are durable** (`port_twilio_sends`, `port_m365_sends`). A send that was claimed but not finished is treated as uncertain and never resent blindly.

### Waiting on the owner

- **Trusted proxies for rate limiting** default to loopback plus private ranges, which suits Caddy in Docker. Set `BIZ_TRUSTED_PROXIES` to narrow it.
- **Missed-call text-back autonomy for HIVE.** It is at level 2 now, so the owner approves each SMS. Raise it to 3 in `apps/hive/policy.yaml` for an instant templated text-back.
- **Real HIVE material** for `apps/hive/memory/` (offer, voice samples, past jobs). The files there are placeholders.
- **Local model choice.** Ollama isn't installed on this dev box. Run `biz eval <task> --tier shadow` on the Mac mini.
- **Claude prices** in `ports/model_claude/biz_port_model_claude/config.py` are placeholders.
- **Microsoft 365 or Google Workspace** (a spec open question). M365 is built; Google isn't.
- **Business time zone** for working hours (`BIZ_M365_TZ`).

### Not done yet

- Mac mini deploy and backups. The compose stack has not been run: there is no Docker on the dev box.
- **Reply handling is subject-token only:** replies are matched by the `[ref:...]` token in the subject (the mail port exposes no In-Reply-To/References), so a customer who rewrites the subject starts a new enquiry.
- **A gate-blocked reply ends the run with `EvidenceMissing`,** so the task stays open for the owner, rather than completing with a `message.blocked` packet.
- **Web shell (`web/`)** isn't started.
