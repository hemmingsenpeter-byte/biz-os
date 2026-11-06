# Deploying BIZ OS on the Mac mini

Everything in `docker-compose.yml` runs in containers except Ollama (native, uses the Apple
GPU) and Tailscale (native). The same compose file runs on any Linux server; only the Ollama
address changes (`OLLAMA_HOST`).

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

First owner: create a `users` row with role `owner` (kernel CLI, when available) and issue a
passkey invite token with `biz_kernel.auth.tokens.issue_invite(user_id)`; open the sign-in page
and enrol a passkey. `BIZ_RP_ID` and `BIZ_ORIGIN` must match the hostname you browse to or
WebAuthn will refuse.

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
