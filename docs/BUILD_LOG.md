# Build log

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
