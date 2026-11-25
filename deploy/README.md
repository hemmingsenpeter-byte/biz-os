# Deploying BIZ OS on the Mac mini

Everything in `docker-compose.yml` runs in containers except Ollama (native, uses the Apple
GPU) and Tailscale (native). The same compose file runs on any Linux server; only the Ollama
address may change (`BIZ_OLLAMA_URL`).

## Local demo smoke check

Run from the repository root with Docker Compose v2 and Python 3 available. This
starts Postgres, the API, worker and owner console using `apps/demo` and fake ports. The API
is checked from inside its container; no tunnel or provider credentials are needed.
Postgres binds to localhost port 5432 and the console to localhost port 3000;
both ports must be free.

```sh
BIZ_DEMO_ENV=$(mktemp)
python3 - <<'PY' > "$BIZ_DEMO_ENV"
import secrets
print("POSTGRES_PASSWORD=" + secrets.token_hex(24))
print("BIZ_SECRET_KEY=" + secrets.token_urlsafe(48))
print("DOCUSEAL_SECRET_KEY_BASE=" + secrets.token_urlsafe(48))
print("BIZ_APP_DIR=/app/apps/demo")
print("BIZ_RP_ID=localhost")
print("BIZ_ORIGIN=http://localhost:3000")
print("CLOUDFLARE_TUNNEL_TOKEN=")
print("ANTHROPIC_API_KEY=")
PY
compose() {
  BIZ_APP_DIR=/app/apps/demo BIZ_RP_ID=localhost BIZ_ORIGIN=http://localhost:3000 \
    docker compose --project-name biz-demo --env-file "$BIZ_DEMO_ENV" \
    -f deploy/docker-compose.yml "$@"
}
compose up --build -d --wait --wait-timeout 120 postgres biz-api biz-worker biz-web
compose exec -T biz-api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/v1/health').read().decode())"
compose exec -T biz-api biz doctor apps/demo
compose exec -T biz-api biz doctor apps/hive
compose cp deploy/smoke_demo.py biz-api:/tmp/smoke_demo.py
compose exec -T biz-api python /tmp/smoke_demo.py --repo-root /app
```

Create the first demo owner and open the printed invitation URL in the same
browser you will use for the console:

```sh
compose exec -T biz-api biz owner-invite apps/demo --email owner@example.invalid --name "Demo Owner"
```

Enrol a passkey, then sign in at `http://localhost:3000/auth/login`. The default
composition starts with an empty approval queue. See the [owner console guide](../docs/OWNER_CONSOLE.md)
for invitation recovery, local development and the seeded browser acceptance flow.

The smoke script creates two randomly named scratch databases using the configured
Postgres server (the database role needs `CREATEDB`). It checks that a demo task
waits for approval with zero sends, kills the workflow process, then recovers and
approves it. Completion must have approval and message evidence. Replaying the
completed workflow must leave exactly one fake mail adapter invocation. It also
renders a PDF and checks its text, exercising the image's Pango libraries and fonts.
Scratch databases are dropped on exit; the composition's database is untouched.
Real provider reconciliation is covered by the port tests, separately from this smoke.

Inspect or restart the core worker in the same shell:

```sh
compose logs --tail 100 biz-worker
compose restart biz-worker
compose logs --tail 100 biz-worker
```

After the check, remove this disposable project's containers, volumes and temporary
credentials:

```sh
compose down --volumes --remove-orphans
rm "$BIZ_DEMO_ENV"
unset BIZ_DEMO_ENV
unset -f compose
```

CI runs the same core startup and smoke script in the `docker-smoke` job. The
Dockerfile accepts an optional BuildKit `proxy_ca` secret containing a CA bundle
for the dependency download step when building behind a TLS-inspecting proxy.

This verifies the packaged demo using disposable credentials and fake providers.
HIVE's live integrations and deployment on the Mac mini have separate acceptance gates.

## 1. macOS setup (once)

- **Never sleep:** System Settings > Energy > Prevent automatic sleeping; or
  `sudo pmset -a sleep 0 disksleep 0 displaysleep 10`.
- **Restart after power failure:** `sudo pmset -a autorestart 1` (Energy > "Start up
  automatically after a power failure").
- **FileVault on.** After a power cut the Mac waits at the unlock screen: unlock it over
  Screen Sharing via Tailscale. The UPS covers short cuts.
- **Wired ethernet, UPS** connected; enable Wake for network access.
- **Tailscale:** install, sign in, enable "Run unattended". Owner and admin reach the mini only
  through it. Do not forward any router ports.
- **Time Machine:** hourly backups to an external SSD (exclude Docker's VM disk and
  `~/biz-backups`; this is private backup staging).
- **Docker:** Docker Desktop or OrbStack, set to start at login; auto-login the service user.
- **Ollama:** `brew install ollama && brew services start ollama`; pull the models you use.
  Containers reach it at `http://host.docker.internal:11434` (set in compose).
  Bind it to `OLLAMA_HOST=0.0.0.0` only if Docker cannot reach the loopback address.

## 2. Configure and preflight

Copy `deploy/.env.example` to `deploy/.env` and restrict it to the operator.
Set `BIZ_APP_DIR` to one composition path inside the image. Set the secrets and
provider settings required by that composition. The stock demo uses fakes.
Use a URL-safe PostgreSQL password such as a generated hexadecimal value.

Set `BIZ_STORAGE_HOST_ROOT` to an existing private directory for a local-storage
composition. Use mode 0700 and grant the API/worker user write access through
the bind mount. The stock images run as UID 1000. Test permissions on the
target Docker installation before using the directory with real documents.
Both application services see the directory at `/var/lib/biz-os/objects`; the
worker and API share this location and backup reads its host path. Stored
documents and recordings are referenced by absolute `file://` URIs, so an
existing install whose URIs use another in-container path must follow
[Migrating stored object paths](#migrating-stored-object-paths) before the new
release starts. Preflight warns when the env file still sets the legacy
`BIZ_STORAGE_ROOT` to a different path.

`BIZ_TRUSTED_PROXIES` lists the proxies whose `CF-Connecting-IP` and
`X-Forwarded-For` headers the API believes when it records a caller's address and
rate-limits public webhooks. Set it to the application network that `caddy` and
`tunnel-ingress` use to reach the API: Compose fixes that subnet with
`BIZ_APP_SUBNET` (default `172.30.250.0/24`), and `.env.example` sets both to the
same value. When it is empty, the API trusts every private range, including other
Docker bridges, and preflight warns. Pick another `BIZ_APP_SUBNET` if the default
overlaps a network on the host, and keep `BIZ_TRUSTED_PROXIES` equal to it.
Upgrading from a release without a fixed subnet, or changing it, recreates the
application network: run `docker compose ... down` (without `--volumes`) and then
`up -d`.

Run the offline preflight from the repository root with Docker Compose v2
available. It resolves the stack's configuration without starting services or
contacting providers:

```sh
uv sync --locked
uv run python -m deploy.preflight --env-file deploy/.env
```

`Preflight failed:` lines block the deployment. `Preflight warning:` lines do not
fail the check, but resolve them before handling real traffic.

### Migrating stored object paths

The local storage adapter records each object as an absolute URI such as
`file:///var/lib/biz-os/objects/ab/<sha256>.pdf`, and it refuses URIs outside
its root. An install created with another in-container path (for example a
Compose override that mounted `BIZ_STORAGE_ROOT=/srv/biz/objects` at the same
path) keeps working only after its stored URIs are rewritten. Files stay where
they are; only the in-container prefix changes. In the commands below,
`compose` means `docker compose -f deploy/docker-compose.yml --env-file deploy/.env`
with any override files the install uses.

1. Settle in-flight workflows and pending approvals that attach documents or
   recordings, then stop the writers: `compose stop biz-api biz-worker`.
2. Take a backup with `bash deploy/backups/backup.sh` and record the snapshot ID.
3. List the object-store prefixes in use. The query matches only URIs shaped
   like stored objects (`file:///<root>/<2 hex>/<sha256><ext>`), so profile
   sources such as `file:hours.md` are not listed. Every row should show one old
   prefix, for example `file:///srv/biz/objects`. Drop the
   `cap_voice_capture_notes` line if that table does not exist:

   ```sh
   compose exec -T postgres psql -U biz -d biz -Atc "
     SELECT DISTINCT regexp_replace(uri, '/[0-9a-f]{2}/[^/]+$', '') FROM documents WHERE uri ~ '^file:///.+/[0-9a-f]{2}/[0-9a-f]{64}(\.[A-Za-z0-9]{1,10})?$'
     UNION SELECT DISTINCT regexp_replace(audio_uri, '/[0-9a-f]{2}/[^/]+$', '') FROM cap_voice_capture_notes WHERE audio_uri ~ '^file:///.+/[0-9a-f]{2}/[0-9a-f]{64}(\.[A-Za-z0-9]{1,10})?$'"
   ```
4. In `deploy/.env`, set `BIZ_STORAGE_HOST_ROOT` to the absolute host directory
   that holds those files (the source of the old mount), keep it mode 0700, and
   delete the `BIZ_STORAGE_ROOT` line. Run preflight and confirm that the legacy
   path warning is gone.
5. Rewrite the prefix in one transaction. Replace `file:///srv/biz/objects` with
   the exact prefix printed in step 3, including any percent-encoding. As in
   step 3, drop the `cap_voice_capture_notes` statement if that table does not exist.
   `memory_sources` is not touched (see below):

   ```sh
   compose exec -T postgres psql -U biz -d biz -v ON_ERROR_STOP=1 \
     -v old='file:///srv/biz/objects' -v new='file:///var/lib/biz-os/objects' <<'SQL'
   BEGIN;
   UPDATE documents SET uri = :'new' || substr(uri, length(:'old') + 1)
     WHERE starts_with(uri, :'old' || '/');
   UPDATE cap_voice_capture_notes SET audio_uri = :'new' || substr(audio_uri, length(:'old') + 1)
     WHERE starts_with(audio_uri, :'old' || '/');
   COMMIT;
   SQL
   ```
6. Rerun the query from step 3. It must print only `file:///var/lib/biz-os/objects`.
   Start the writers with `compose up -d biz-api biz-worker`, open a stored
   document from the owner console, and take a fresh backup.

Audit events, memory sources and completed workflow results keep the old URI
as history and are not rewritten. The audit log is hash-chained, and
`memory_sources` is append-only: a trigger rejects every update, so including it
would roll back the whole transaction. Old citations still resolve, because a
citation points at its memory source row, which stores the cited text and its
SHA-256. The excerpt and quote come from that row, never from the file. The
old `file://` URI on a source is shown only as a historical label. To open the
file itself, use the document record, whose URI step 5 rewrote. To roll back, stop the
writers and restore the step 2 snapshot as described in
[Back up and recover](#5-back-up-and-recover-the-compose-install).

Start PostgreSQL first. For a fresh installation using DocuSeal, initialize
its database once before starting the service:

```sh
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --wait postgres
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec postgres createdb -U biz docuseal
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl --fail http://127.0.0.1:8080/v1/health
```

The owner console is accessed via the Tailscale Serve HTTPS hostname.
`BIZ_RP_ID` and `BIZ_ORIGIN` must match that hostname and origin for WebAuthn.
Run `biz owner-invite` inside the API container with the selected composition
and open the invitation from an authorized tailnet device.

Update image pins deliberately. Inspect the replacement image's multi-platform
manifest and record its digest, confirm Linux amd64 and arm64, back up the
current installation, then rerun preflight, container smoke, and restore
acceptance against the proposed versions. Keep the previous release and pins
for rollback. The application Dockerfiles also pin their base images.

## 3. Owner and public ingress

The private Caddy listener is bound to host loopback at port 8080. On the host,
run `tailscale serve --bg http://127.0.0.1:8080` and grant access only to
authorized owner devices in the tailnet ACL. Keep Tailscale Funnel disabled.
PostgreSQL, the demo web port, DocuSeal administration and Kuma also bind
only to loopback. Verify these bindings on the appliance, and use no router
port forwards.

When inbound webhooks are approved, configure the Cloudflare hostname to
use `http://tunnel-ingress:8081`. Start the separate profile with
`docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile public up -d`.
The tunnel container shares a bridge with the public gateway and has no
attachment to the application network. On every hostname the public gateway
exposes the API only through `POST /webhooks/telephony` and `POST /webhooks/form`.
All other API and console paths return 404, including `/v1` and the owner console.
Verify the public hostname fails closed and that the tailnet origin serves `/v1/health`.

Outside signers open DocuSeal links on a separate signing hostname. Set it only
when the composition binds the `sign_docuseal` port: put the hostname in
`DOCUSEAL_HOST` and route it in Cloudflare to the same `http://tunnel-ingress:8081`
origin. Compose then sets DocuSeal's `APP_URL` to `https://<DOCUSEAL_HOST>`, so the
links the port returns are `https://<DOCUSEAL_HOST>/s/<slug>`. Preflight checks this
and flags `DOCUSEAL_HOST` as unused for a composition without the signing port.
With `DOCUSEAL_HOST` empty, the gateway's signing routes match no request.

On the signing hostname, the gateway forwards only what the pinned DocuSeal
signing page needs:

| Paths | Methods | Purpose |
| --- | --- | --- |
| `/s/*` | GET, HEAD, POST, PUT, PATCH | Submitter form, its field values, uploads, decline, completion and document download |
| `/d/*` | GET, HEAD, POST, PUT, PATCH | Shared template form and its completion page |
| `/p/*` | GET, HEAD, POST, PUT, PATCH | Phone signature pad opened from the form's QR code |
| `/submit_form_email_2fa`, `/start_form_email_2fa_send`, `/resubmit_form` | GET, HEAD, POST, PUT, PATCH | E-mail verification codes and re-signing |
| `/packs/*`, `/fonts/DancingScript-Regular.otf`, favicons | GET, HEAD | Form scripts, styles and the typed-signature font |
| `/preview/*`, `/disk/*`, `/blobs_proxy/*` | GET, HEAD | Signed page images and signed file links |

Administration, sign-in, setup, settings, templates and `/api` return 404 there.
Caddy matches the decoded, cleaned path, but DocuSeal routes the raw one. The
gateway therefore returns 404 for any signing request whose raw path (everything
before the first `?`) contains `#`, an escaped dot, slash, backslash, percent
sign or `#`, a backslash, `//` or a dot-segment. Go keeps a raw `#` in the path,
so it cannot hide what follows. The one exception is `%2F` inside `/disk/` keys,
and only away from slashes and dot-segments. The gateway also refuses a `_method`
query parameter or an `X-HTTP-Method-Override` header. These guards alone prevent
traversal. The form and asset handlers also rewrite to
`{http.request.uri.path}`, but that only canonicalizes escapes and does not
clean dot-segments. A `_method` field in a form body cannot be inspected at the
gateway. Rails can use it only to change the method on the same allowed path, and
no signer route accepts DELETE.
Preflight fails if a DocuSeal route loses these guards or its guard pattern differs
from the expected one.
The signing form tries some legacy `/api/...` endpoints only after a `/s/...`
request returns 404, so the pinned version does not need them. When updating the
DocuSeal pin, review its `config/routes.rb` and the form pack for new signer paths.
Because `APP_URL` becomes the public origin, absolute links in the loopback
DocuSeal administration also point at the signing hostname.
Configure provider URLs against the public hostname only when connecting
the corresponding inbound provider. A DocuSeal service in this Compose
project uses the internal callback `http://biz-api:8000/webhooks/documents/sign`
with its configured shared-secret header.

## 4. Monitoring

Uptime Kuma binds to `http://127.0.0.1:3001`. Access it locally or over a
tailnet-authorized proxy. Monitor private health and the public webhook
boundary independently, and configure operator alerts in the install's runbook.

## 5. Back up and recover the Compose install

| State | Recovery source |
| --- | --- |
| `biz` | Application dump, including approvals, passkeys, audit, and dispatch ledgers |
| `biz_dbos` | DBOS system dump, including workflow state and step results |
| `docuseal` and its `/data/docuseal` named volume | Database dump and file archive |
| Local documents mounted from `BIZ_STORAGE_HOST_ROOT` | File archive; preserve the container adapter path so stored file URIs resolve |
| Release, composition, Compose overrides, Caddy/tunnel routing, and monitor definitions | Retain the deployed Git revision, image versions or digests, and non-secret site overrides in the operator's off-box release records |
| Database credentials, signing/encryption keys, restic access and password, tunnel and provider credentials | Re-provision from the owner's off-box secret store. Keep the existing application and DocuSeal keys and WebAuthn origin/RP ID for recovery |
| Database cluster, container layers, Ollama model cache, Caddy state and Kuma state | Recreate from the release, re-pull the chosen models, reissue certificates, and rebuild monitoring using the saved configuration |

The scripts cover the standard Compose topology, where PostgreSQL role `biz` owns
all three databases on the `postgres` service. An external DBOS database, more
databases, or a separate document store requires a different recovery procedure.

### Back up

1. Install Docker Compose v2, restic 0.18 or newer, Python 3 and `shasum`. Create
   the encrypted off-box repository once with `restic init` using the intended
   credentials. The backup job checks that it can open the repository and reports
   an error if it cannot.
2. Set `BIZ_STORAGE_HOST_ROOT` to the absolute host directory mounted into the
   API and worker. For a demo with fake storage, provision an empty private
   directory. The backup script also supports the legacy host variable
   `BIZ_STORAGE_ROOT` for a Compose override that still uses that layout. Both
   scripts refuse to run unless this directory is the object bind mount that the
   resolved Compose configuration and any existing API and worker containers use.
3. Put the restic settings and document path in the trusted, shell-compatible
   `deploy/.env` and restrict its permissions. Set `BIZ_DUMP_DIR` for private working
   space if needed (default `~/biz-backups/dumps`). Leave free space for staging and
   one full download verification under `TMPDIR`. `BIZ_ENV_FILE`, `BIZ_COMPOSE_FILE`,
   `BIZ_COMPOSE_OVERRIDE_FILE` and `COMPOSE_PROJECT_NAME` select an alternate install.
   `BIZ_BACKUP_HELPER_IMAGE` optionally selects a locally available image with
   `tar` and `find` for the network-isolated volume helper. It defaults to the
   running PostgreSQL container's image.
4. In a maintenance window, disable ingress, drain active work, and stop the API,
   worker and DocuSeal. Keep PostgreSQL running. Keep the stopped DocuSeal container
   so the script can identify its named volume. Suspend any other process that can
   write the databases or object directories. Keep these writers stopped until the
   backup command exits. For the default project:

   ```sh
   docker compose -f deploy/docker-compose.yml --env-file deploy/.env stop biz-api biz-worker docuseal
   bash deploy/backups/backup.sh
   ```
5. Record the full snapshot ID from `backup ok <id>` and resume the maintenance
   window's services according to the install's runbook. A failed run exits nonzero
   and names the failed stage on stderr. Alert on nonzero status or a missing success
   marker. Retention runs after full download and byte comparison (14 daily, 8
   weekly, 12 monthly by default, with at least one per host/path group). Disk space
   reclamation via `restic prune` is separate repository maintenance.

The launchd plist is a scheduling template. Use it only when the maintenance window
also quiesces and restarts the writers and someone monitors the job. If a power
failure leaves private staging or a lock behind, confirm that neither command is
running and remove the stale staging directory and lock before retrying.

### Recover on a clean replacement

1. Recover the deployment release and credentials and select an ID from a recorded
   successful backup. Keep all real provider traffic and ingress disabled.
2. Use a fresh Compose project and fresh volumes, with the same PostgreSQL major
   version, extensions and role. In a recovery Compose override, set
   `services.postgres.environment.POSTGRES_DB: postgres` so Compose creates no
   application databases. Set `BIZ_STORAGE_HOST_ROOT` in `deploy/.env` to the same
   absolute directory as the backed-up install and make it empty. The restore script
   checks that Compose will mount it into the API and worker.
   Start only PostgreSQL, then create the stopped DocuSeal container with
   `docker compose ... create docuseal`. Use the same project/env/file
   selections for those commands and the restore script.
3. Run `bash deploy/backups/restore.sh FULL_SNAPSHOT_ID`. It verifies the snapshot
   and requires empty target databases and file stores before writing. On failure,
   retain the backup and rebuild the disposable target before retrying. A failed
   restore can have applied some of the data.
4. Verify restored approvals, in-flight workflows, the audit chain and sample
   documents while isolated. Start with the backed-up build and DBOS workflow version.
   Check provider state for external actions since the snapshot before enabling any
   recovered dispatch. An older snapshot can lack evidence about actions already
   accepted by a provider. Resume ingress after the operator has reconciled them.

### Disposable acceptance drill

Run `uv sync --locked` and then `uv run python -m deploy.restore_drill` from the
repository root with Docker Compose v2 and restic on PATH. The command uses
disposable PostgreSQL/DBOS projects, a local encrypted restic repository and
fake delivery. It kills the source workflow while approval is pending, destroys
the source project and empties its object root, then restores all three
databases and both file stores. It verifies the audit chain and approval, then
approves and replays the workflow and checks for exactly one fake delivery.
On 20 November 2026, the drill measured a 7.42 s recovery-point age at simulated
loss and 30.85 s from simulated loss through verified recovery and replay. There
were zero fixture writes after the recorded checkpoint. These measurements used
a small synthetic workload and local restic. Measure again with the appliance,
off-box destination and representative data before setting production targets.

## 6. Whisper (M4)

The speech adapter runs inside the application. Transcribing audio requires the
optional `whisper` extra and a locally available model. The standard demo
uses the fake speech adapter.

## 7. Receptionist shadow mode

HIVE starts in shadow mode with the receptionist as its active role. Compose
passes the same execution mode to the API and worker. An empty
`BIZ_EXECUTION_MODE` selects the composition default. The variable can only make
the mode stricter: `supervised` over a shadow composition is refused at startup,
by `biz doctor` and by preflight. A composition runs supervised only when its
`supervised_authorization` records `authorized_by`, `authorized_on` and
`pilot_exit_evidence`. In shadow mode a cloud model port is refused unless the
policy sets `shadow.allow_cloud_model: true`. The demo records a fixture
authorization and keeps its supervised fake-provider behavior.

In shadow mode, the runtime can collect enquiries, classify them and prepare
internal drafts. The dispatch guard stops external and money actions, including
a previously approved staged action. Suppressions are recorded as
`shadow.suppressed` audit events with the verdict policy would have given. A
suppressed approved action is terminal (`shadowed`): it leaves the approval
queue and never runs, even after the mode becomes supervised. Workflow suppressions appear on the task
evidence timeline and leave the task open for owner review. Use an owner
passkey to access the console.

External provider credentials are provisioned separately at the approved access
and pilot gates. Preflight requires on-appliance Ollama for shadow Compose
deployments. Runtime startup and `biz doctor` also validate the resolved Ollama
endpoint and both generation and embedding model names before inference. Host runs
accept loopback or the appliance Docker host; Compose preflight continues to require
`host.docker.internal` and noncloud models. The existing cloud policy permission does
not relax Compose's appliance restrictions. Appliance inference bypasses inherited
HTTP proxies. These offline checks do not replace verifying the trusted Ollama server
and the installed model's actual routing. Verify the approved model is installed on the Mac mini before
connecting a mailbox. Before replacing an older release, settle in-flight work
for the inactive roles and stop the old API and workers. Update and restart
both application services together. The supervised transition requires pilot
exit evidence and the owner's authorization. Exclude approvals for suppressed
actions from supervised quality and autonomy evidence.
