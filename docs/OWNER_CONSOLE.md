# Owner console

The Next.js app under `web/` provides passkey enrolment and sign-in, an approval
queue, exact action details, approve/edit/reject actions, task status and recorded
completion evidence, a pause control and a scorecard. It uses the existing kernel
API and session cookie.

## Start the packaged demo

Follow the [local Docker demo setup](../deploy/README.md#local-demo-smoke-check).
It starts the web app at `http://localhost:3000` with fake ports and creates a
temporary environment file for the disposable stack. The guide's `compose`
function explicitly selects `apps/demo`.

After the worker has migrated the database, create the first owner:

```sh
compose exec -T biz-api biz owner-invite apps/demo --email owner@example.invalid --name "Demo Owner"
```

Open the printed URL, choose **Create passkey**, and complete your browser's
passkey prompt. Continue to sign in using the same email and passkey. The default
demo starts without pending approvals; the browser acceptance test below seeds a
complete review scenario.

The invitation is a bearer credential, valid for 24 hours and consumed when
enrolment begins. Keep its URL private. If the prompt is cancelled, the page can
retry its active challenge. After a page refresh or expired challenge, issue a
fresh invitation for the same active owner:

```sh
compose exec -T biz-api biz owner-invite apps/demo --email owner@example.invalid --reissue
```

Reissuing creates another invitation; it does not revoke earlier unconsumed links.
The command refuses an implicit promotion of an existing staff user, a second
first owner, or recovery for an inactive owner. It commits the owner record before
printing the invitation, and records the administration action without its token.
`--json` returns the invitation and user ID for local automation.

## Review work

- Open **Approvals** to see pending requests. Each detail page displays the
  exact inputs, revision, requesting role, reason and linked task.
- **Approve version N** sends that revision to the existing approval API.
  A changed revision returns a conflict: the page refreshes its details and asks
  you to review them before making a new decision. It never retries approval
  automatically.
- **Reject request** opens a reason form. Confirming rejection sends no action;
  the task stays open for follow-up.
- Open the linked task to see its status and recorded evidence. Activity includes
  evidence belonging to that task and applicable job-level evidence. Sibling-task
  activity and unrelated jobs are excluded.

## Pause everything

- **Pause everything** is in the header of every workspace page. It asks for one
  confirmation and an optional reason. Any signed-in user can pause.
- While paused, every page shows a red banner: who paused, when and why. No role
  starts new work, nothing is sent and nothing can be approved. Incoming work is
  still collected and runs after the resume.
- **Approve version N** is disabled while paused, with the reason beside it.
  Editing and rejecting stay available. If the pause came from someone else while
  a request was open, the refused approval says so instead of reporting a changed
  revision.
- **Resume** is on the banner and is for the owner only. Staff see that the owner
  must resume. The page rechecks the pause state on each navigation, when the
  window regains focus and every 30 seconds.

## Edit a proposal

- **Edit proposal** on a pending request opens its text values for editing. Other
  values are shown and sent back unchanged. The operation reference is never
  editable; the server keeps the original, so an edit cannot cause a second send.
- A reason in one line and the kind of change are required: **Wording or tone
  only** (`cosmetic`), **A fact was wrong** (`factual`) or **It broke one of our
  rules** (`policy`). These corrections are what the roles are trained on.
- **Save as version N** stores the edit as a new pending revision. It approves
  nothing: review the new revision, then use **Approve version N** as usual.
- Request text can come from outside people. It is displayed and edited strictly
  as text.

## Scorecard

- Open **Scorecard** for the last 28 days: tasks completed, serious errors, cost,
  cost per task and time saved.
- The table lists each role and type of decision: proposals, approved as is,
  approved with wording-only edits, approved with errors (a factual, rule or
  untagged edit), rejected and waiting, then the approved-as-is and error-free
  rates. Both rates count approved proposals only.
- **Not measured yet** means there is nothing to measure from, such as no approved
  proposal, no completed task or no timed baseline. It is never shown as 0.
- **Recent corrections** lists the latest 20 edits: the draft, the edit, the
  reason and the kind of change.

## Local development

Use Node 24, pnpm 11.25.0 and the repository's Python 3.12 environment. Start a
migrated API and worker against a local development database, then run:

```sh
pnpm --dir web install --frozen-lockfile
BIZ_API_URL=http://127.0.0.1:8000 BIZ_ORIGIN=http://localhost:3000 pnpm --dir web dev
```

Set the API's `BIZ_RP_ID=localhost` and `BIZ_ORIGIN=http://localhost:3000` too.
The browser always calls `/v1` on the web origin; the server forwards requests to
the private API using runtime `BIZ_API_URL`. Session cookies remain HttpOnly.
Use the hostname and port configured in `BIZ_ORIGIN` rather than switching between
`localhost` and `127.0.0.1`. Production passkeys require HTTPS and the matching
relying-party hostname.

Browser mutations are checked against `BIZ_ORIGIN` by both the web proxy and API.
Cross-origin and sibling-origin browser requests are refused. Headerless API
clients retain their existing authentication requirements. Provider webhooks keep
their existing signature verification path.

## Verification

```sh
pnpm --dir web lint
pnpm --dir web typecheck
pnpm --dir web build
pnpm --dir web exec playwright install chromium
pnpm --dir web test:e2e
```

The browser test starts disposable Postgres, DBOS, the production API and built
Next.js app. It uses only fake ports and its own generated credentials, independent
of the caller's database configuration. Chromium's virtual authenticator performs
real WebAuthn enrolment and sign-in. The test reviews exact message contents,
approves once, verifies completion evidence, rejects another request and checks a
stale-version conflict. Refresh/retry must leave one fake mail invocation. It then
pauses, checks the banner on every page and the blocked approval, resumes, edits a
proposal with a reason and kind of change, approves the new revision, checks that
exactly the edited text is sent under the original operation reference, and reads
the scorecard with the correction listed, before signing out. Desktop and mobile
screenshots go to ignored `web/test-results/`.

On a development machine with Chromium already installed, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium` instead of downloading a test
browser. The harness stops its servers and removes its temporary databases and
invitation state on exit. CI also builds and starts the packaged web container.
