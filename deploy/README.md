# Deploying BIZ OS on the Mac mini

Everything in `docker-compose.yml` runs in containers except Ollama (native, uses the Apple
GPU) and Tailscale (native). The same compose file runs on any Linux server; only the Ollama
address changes (`OLLAMA_HOST`).

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
print("BIZ_DOMAIN=localhost")
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

This verifies the packaged demo. HIVE's live integrations, Mac mini deployment,
backup acceptance and customer portal still need their own acceptance checks.
The current Compose environment mapping also needs the relevant provider variables
from `.env` wired into the API and worker before enabling HIVE's real ports.

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
  `~/biz-backups`; they are covered by the nightly dump).
- **Docker:** Docker Desktop or OrbStack, set to start at login; auto-login the service user.
- **Ollama:** `brew install ollama && brew services start ollama`; pull the models you use.
  Containers reach it at `http://host.docker.internal:11434` (set in compose).
  Bind it to `OLLAMA_HOST=0.0.0.0` only if Docker cannot reach the loopback address.

## 2. Configure and start

```
cp deploy/.env.example deploy/.env      # fill in every value; never commit .env
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl -s http://localhost/v1/health      # via Caddy
```

Create the DocuSeal database once: `docker compose exec postgres createdb -U biz docuseal`.

First owner: run `biz owner-invite` inside the API container against the migrated
database, then open the printed invitation and enrol a passkey. For example:
`docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec biz-api biz owner-invite apps/hive --email <owner-email> --name <owner-name>`.
`BIZ_RP_ID` and `BIZ_ORIGIN` must match the hostname and full origin you browse to
or WebAuthn will refuse. `BIZ_ORIGIN` also guards browser API mutations, including
requests that reach the API directly through Caddy.

## 3. Public access (Cloudflare Tunnel)

Create a tunnel in Cloudflare Zero Trust, put its token in `CLOUDFLARE_TUNNEL_TOKEN`, and add
public hostnames following `cloudflared.config.example.yml`: webhooks and link pages only.
`/v1` stays private (Tailscale). Missed-call text-back is also configured inside Twilio, so
callers get a reply even when the Mac is down.

## 4. Monitoring

Uptime Kuma at `http://<tailscale-name>:3001`. Add monitors for `/v1/health`, Postgres, Caddy
and the tunnel hostname; send alerts to the owner by SMS or push.

## 5. Backups

`backups/backup.sh` runs `pg_dump -Fc` for `biz` and `docuseal`, then `restic backup` to B2 or
R2 (credentials in `.env`), prunes, and spot-checks 5% of the data.

```
mkdir -p ~/biz-backups
sed -i '' "s#CHANGE_ME#$(whoami)#g" deploy/backups/nz.biz-os.backup.plist
cp deploy/backups/nz.biz-os.backup.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/nz.biz-os.backup.plist
launchctl start nz.biz-os.backup        # test run
```

Restore drill (do it quarterly): `restic restore latest --target /tmp/r`, then
`pg_restore -U biz -d biz --clean /tmp/r/.../biz-*.dump` inside the postgres container.

## 6. Whisper (M4)

The `whisper` service is commented out in compose. Uncomment it when the speech capability
lands, or run faster-whisper natively.
