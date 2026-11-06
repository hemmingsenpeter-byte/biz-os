# BIZ OS: handover note from the cloud session (2026-11-06, evening)

This picks up from the Mac's handover note of 2026-11-06, 18:07–21:06.

## Where the work is

- **Branch:** `claude/elegant-hypatia-2ionjc` on GitHub. No pull request is open.
- **Base:** `59aa879`, the same commit as `origin/main`.
- **Not included:** the cloud session started from a fresh clone, so it never had the Mac's two unpushed commits (`ea79969` documents fixes, `922f0f0` reply-thread and upload fixes) or the stopped agent's uncommitted quoting work.
- **Two new commits on the branch:**

| Commit | What it does |
| --- | --- |
| `fd480a9` | Open defect #1 (quoting margin bypass), fixed |
| `cd65c60` | Open defect #2 (intake read the comms outbox with raw SQL), fixed |

- **Checks on the branch:**
  - Full suite: 400 passed, 0 failed, including the PDF tests, because Pango is installed in the cloud container.
  - ruff, ruff format and `mypy kernel/biz_kernel capabilities ports` are clean.
  - `biz doctor apps/hive` and `biz doctor apps/demo` show no failures.

## First thing on the Mac tomorrow

1. Discard the stopped agent's partial quoting work. `fd480a9` replaces it.

```bash
git restore capabilities/quoting roles/estimator kernel/tests/capabilities/test_quoting.py tests/e2e/test_estimator.py
```

2. Fetch the branch.

```bash
git fetch origin claude/elegant-hypatia-2ionjc
```

3. Merge it into the local `main`, which still holds `ea79969` and `922f0f0`.

```bash
git merge origin/claude/elegant-hypatia-2ionjc
```

4. **Expect a conflict in `capabilities/intake/biz_cap_intake/web.py`** (and maybe `kernel/tests/capabilities/test_comms.py`), because `922f0f0` changed reply threading there too.
   - Keep the new shape: `match_reply(rt, session, sender, subject)` calls the `comms.match_reply` tool. It no longer runs SQL, and the `has_table` check is gone.
   - Re-apply whatever `922f0f0` changed on top of that.
   - If `922f0f0` added rules for matching a reply to its thread, they now belong in `comms.match_reply` in `capabilities/comms/biz_cap_comms/tools.py`.
5. Run the reviewer's proof tests (`review3/test_quoting_bypass.py` in the Mac session scratchpad) against the merged code. The cloud session couldn't see them. It wrote its own regression tests instead (see below).
6. Run the full suite, mypy and `biz doctor apps/hive`, then push.

## What changed

### Defect #1: the margin check no longer trusts the model (`fd480a9`)

Files: `capabilities/quoting/biz_cap_quoting/{service,tools}.py`, `roles/estimator/{sops,prompts}/*`, `kernel/tests/capabilities/test_quoting.py`.

- **Costs come only from the price book.** For a line with a price-book code, `price_lines` ignores any `unit_cost` the caller supplies and adds a `cost_override_ignored` warning. Price overrides still work, with a `price_override` warning, and the margin check covers them.
- **Negative values are refused** with a `negative_value` error: negative `unit_cost`, `unit_price`, `discount_pct` and `contingency_pct`, and any discount of 100% or more.
  - Negative quantities are refused on estimates.
  - They are still allowed on **variations**, because the variation prompt uses them for removed work, which becomes a credit. A variation that is all credit already fails the margin check with `zero_total`.
- **Any unpriced line makes `margin_ok` false** (`unpriced_blocks_margin`), on estimates and variations alike. In the pricing and variation SOPs, an unpriced line therefore escalates to the owner.
- **`estimate_check` recomputes everything from the stored lines** and doesn't trust the stored totals. The check fails if any of these is true:
  - a line's totals don't recompute from its quantity and unit prices (`line_mismatch`);
  - a line's cost isn't its price-book basis (`cost_basis_mismatch`);
  - the recomputed totals differ from the stored totals (`totals_mismatch`).

  It also warns when the scope has been superseded.
- **`proposal_build` refuses** to build from an estimate that:
  - has been superseded (`estimate_not_current`);
  - prices a superseded scope (`scope_not_current`);
  - fails the margin re-check (`margin_not_ok`).
- **Estimator role data:**
  - The pricing and variation prompts no longer ask the model for costs.
  - The draft schemas drop `unit_cost` from the required fields, add `unpriced: boolean` per line, and set `unit_price` to a minimum of 0.
  - This also fixes a latent crash: an unpriced line with `code: null` and no `unpriced` flag used to make `estimate_write` raise.
- **Tests:** the existing versioning test now expects `proposal_build` to refuse the superseded estimate. New tests cover:
  - a model-supplied cost being ignored;
  - 7 kinds of negative value;
  - a negative quantity hiding a low margin;
  - unpriced lines blocking the estimate check and `proposal_build`;
  - unpriced and negative variations;
  - a proposal refused below margin, and on a superseded scope;
  - tampered stored totals and line costs being caught by `estimate_check`.

### Defect #2: intake uses a published comms tool (`cd65c60`)

Files: `capabilities/comms/biz_cap_comms/tools.py`, `capabilities/comms/capability.yaml`, `capabilities/intake/biz_cap_intake/web.py`, `kernel/tests/capabilities/test_comms.py`.

- **New read-only tool `comms.match_reply(reply_token, sender)`.** It returns `{matched, message_id, party_id, enquiry_id}` for a sent mail with that token, but only when the sender (normalised) is the address the mail went to. The tool is declared in `capability.yaml`.
- **Intake calls the tool through `rt.registry.call(...)`** with `actor="intake"` instead of querying `cap_comms_outbox`. When comms isn't registered, nothing matches, so the mail is handled as a new enquiry.
- **Tests:** the comms tool list now includes `comms.match_reply`. The `match_reply` test runs through the tool, and covers a registry with no comms and an empty token.

## Still open

### Decisions only the owner can make

- **Defect #3, sender verification (always false today).** Fixing it means adding authentication results (from the `Authentication-Results` header) to the mail port's inbound message. That changes a port interface, and CLAUDE.md says to ask first.
- **`SignPort`:** whether to add an optional `idempotency_key`. This is still open from the Mac's note.
- **Everything else in the Mac's "Decisions you need to make" list is unchanged:** HIVE placeholders, memory files, settings, licence, and the three booking approvals.

### Ideas for next work that wouldn't clash with the Mac's commits

None of these has been started.

- **Run the Docker Compose stack in a cloud session.** The Docker client is installed there, but nobody has checked whether the Docker daemon is reachable. This would be the first real run of `deploy/docker-compose.yml`, with no code changes.
- **Start M6 (money).** This needs a new `accounting_xero` port contract, a fake, and the invoicing, payables and cash capabilities, starting at migration `0014`. It touches money actions, so get the owner's go-ahead before building it.
- **Tidy-up:** set `asyncio_default_fixture_loop_scope` in the pytest config to silence the pytest-asyncio deprecation warning.

### Not touched

- `docs/BUILD_LOG.md`, left alone to avoid clashing with the Mac's commits. Add an entry for these two fixes after merging.
- `README.md` status.
