#!/usr/bin/env bash
# Take a consistent, encrypted snapshot of a quiesced standard Compose install.
set +x
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/common.sh"

require_stopped_writers
verify_installed_object_mounts
find_docuseal_volume
restic snapshots >/dev/null 2>&1 || fail "cannot access the provisioned restic repository"

mkdir -- "$DUMP_DIR/stage" || fail "cannot create the staging directory"
STAGE="$DUMP_DIR/stage"
TEMP="$(mktemp -d)" || fail "cannot create the verification directory"
for db in "${DBS[@]}"; do
  out="$STAGE/$db.dump"
  dc exec -T postgres pg_dump -U biz -Fc "$db" >"$out" 2>/dev/null ||
    fail "PostgreSQL dump failed for $db"
  dc exec -T postgres pg_restore --file=/dev/null <"$out" >/dev/null 2>&1 ||
    fail "invalid PostgreSQL dump for $db"
done
tar -C "$STORAGE" -cpf "$STAGE/objects.tar" . 2>/dev/null ||
  fail "local object archive failed"
docker run --rm --network none \
  --mount "type=volume,src=$VOLUME,dst=/data/docuseal,readonly,volume-nocopy" \
  --entrypoint tar "$IMAGE" -C /data/docuseal -cpf - . >"$STAGE/docuseal.tar" 2>/dev/null ||
  fail "DocuSeal volume archive failed"
for archive in objects docuseal; do
  tar -tf "$STAGE/$archive.tar" >/dev/null 2>&1 || fail "invalid $archive archive"
done
require_stopped_writers
manifest_for "$STAGE" >"$STAGE/recovery.txt" 2>/dev/null || fail "cannot checksum staging files"

snapshot_tag="backup-${TEMP##*/}"
restic backup --json --tag "biz-nightly,$snapshot_tag" "$STAGE" >"$TEMP/backup.json" 2>/dev/null ||
  fail "encrypted snapshot upload failed"
snapshot_id="$(python3 -c '
import json, sys
for line in sys.stdin:
    event = json.loads(line)
    if event.get("message_type") == "summary":
        print(event["snapshot_id"])
' <"$TEMP/backup.json" 2>/dev/null)" || fail "cannot read the uploaded snapshot ID"
[[ "$snapshot_id" =~ ^[0-9a-f]{64}$ ]] || fail "missing or invalid uploaded snapshot ID"

restic restore "$snapshot_id" --target "$TEMP/verified" --verify >/dev/null 2>&1 ||
  fail "cannot retrieve and verify the uploaded snapshot"
for file in "${FILES[@]}" recovery.txt; do
  cmp -s "$STAGE/$file" "$TEMP/verified$STAGE/$file" ||
    fail "uploaded snapshot differs from staging ($file)"
done
restic check >/dev/null 2>&1 || fail "restic repository integrity check failed"

# Forget only after full verification. Reclaiming unreferenced bytes is a separate
# maintenance operation, so this job never prunes data or rotates local legacy dumps.
# Keep the verified snapshot by its unique tag even if the system clock moved.
restic forget --tag biz-nightly --group-by host,paths --keep-tag "$snapshot_tag" --keep-last 1 \
  --keep-daily "${KEEP_DAILY:-14}" --keep-weekly "${KEEP_WEEKLY:-8}" \
  --keep-monthly "${KEEP_MONTHLY:-12}" >/dev/null 2>&1 ||
  fail "verified snapshot uploaded, but snapshot retention failed"
restic cat snapshot "$snapshot_id" >/dev/null 2>&1 ||
  fail "verified snapshot is unavailable after retention"

# Finish cleanup before emitting the success marker.
trap - EXIT
cleanup || fail "cannot clean up backup staging"
printf 'backup ok %s\n' "$snapshot_id"
