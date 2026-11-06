#!/usr/bin/env bash
# Nightly: pg_dump of every database in the postgres container, then restic to B2/R2.
# Config comes from deploy/.env (RESTIC_REPOSITORY, RESTIC_PASSWORD, credentials).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${BIZ_ENV_FILE:-$HERE/../.env}"
COMPOSE_FILE="${BIZ_COMPOSE_FILE:-$HERE/../docker-compose.yml}"
DUMP_DIR="${BIZ_DUMP_DIR:-$HOME/biz-backups/dumps}"
KEEP_DAILY="${KEEP_DAILY:-14}" KEEP_WEEKLY="${KEEP_WEEKLY:-8}" KEEP_MONTHLY="${KEEP_MONTHLY:-12}"

# launchd gives a minimal PATH; Homebrew lives here on Apple silicon
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

mkdir -p "$DUMP_DIR"
stamp="$(date +%Y%m%dT%H%M%S)"
dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

for db in biz docuseal; do
  out="$DUMP_DIR/$db-$stamp.dump"
  dc exec -T postgres pg_dump -U biz -Fc "$db" > "$out.tmp" || { rm -f "$out.tmp"; continue; }
  mv "$out.tmp" "$out"
done

# Keep a week of local dumps; the restic repository holds the long history
find "$DUMP_DIR" -name '*.dump' -mtime +7 -delete

restic snapshots >/dev/null 2>&1 || restic init
restic backup "$DUMP_DIR" --tag biz-nightly
restic forget --prune --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY"
restic check --read-data-subset=5%
echo "backup ok $stamp"
