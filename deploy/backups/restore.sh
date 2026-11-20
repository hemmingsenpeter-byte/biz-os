#!/usr/bin/env bash
# Restore only to a clean standard Compose install. Select a verified full snapshot ID.
set +x
set -euo pipefail
[[ "${1:-}" =~ ^[0-9a-f]{64}$ && "$#" == 1 ]] || {
  printf 'Usage: bash %s FULL_SNAPSHOT_ID\n' "$0" >&2
  exit 2
}
snapshot_id="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/common.sh"

require_stopped_writers
verify_installed_object_mounts
find_docuseal_volume
require_empty_target() {
  local databases objects docuseal
  databases="$(dc exec -T postgres psql -XAt -v ON_ERROR_STOP=1 -U biz -d postgres \
    -c "SELECT datname FROM pg_database WHERE datname IN ('biz', 'biz_dbos', 'docuseal')" \
    2>/dev/null)" || fail "cannot check clean target databases"
  [[ -z "$databases" ]] || fail "target databases already exist"
  objects="$(find "$STORAGE/." ! -name . -print -quit 2>/dev/null)" ||
    fail "cannot check the target local object directory"
  [[ -z "$objects" ]] || fail "target local object directory is not empty"
  docuseal="$(docker run --rm --network none \
    --mount "type=volume,src=$VOLUME,dst=/data/docuseal,readonly,volume-nocopy" \
    --entrypoint find "$IMAGE" /data/docuseal/. ! -name . -print -quit 2>/dev/null)" ||
    fail "cannot check the target DocuSeal volume"
  [[ -z "$docuseal" ]] || fail "target DocuSeal volume is not empty"
}

require_empty_target
TEMP="$(mktemp -d)" || fail "cannot create the recovery directory"
restic restore "$snapshot_id" --target "$TEMP" --verify >/dev/null 2>&1 ||
  fail "cannot retrieve and verify the selected snapshot"
# Restic preserves the original absolute staging path. Locate it independently of
# this install's working directory. Accept exactly one manifest.
manifest="$(find "$TEMP" -type f -name recovery.txt -print 2>/dev/null)" ||
  fail "cannot locate the recovery manifest"
[[ -n "$manifest" && "$manifest" != *$'\n'* ]] || fail "expected exactly one recovery manifest"
source_dir="${manifest%/*}"
manifest_for "$source_dir" >"$TEMP/expected.txt" 2>/dev/null ||
  fail "cannot checksum recovered files"
cmp -s "$TEMP/expected.txt" "$manifest" ||
  fail "recovered files or the absolute object root do not match the manifest"

require_stopped_writers
require_empty_target
for db in "${DBS[@]}"; do
  dc exec -T postgres pg_restore -U biz --dbname=postgres --create --exit-on-error \
    <"$source_dir/$db.dump" >/dev/null 2>&1 || fail "database restore failed for $db"
done
tar -C "$STORAGE" -xpf "$source_dir/objects.tar" >/dev/null 2>&1 ||
  fail "local object restore failed"
docker run --rm -i --network none \
  --mount "type=volume,src=$VOLUME,dst=/data/docuseal,volume-nocopy" \
  --entrypoint tar "$IMAGE" -C /data/docuseal -xpf - <"$source_dir/docuseal.tar" \
  >/dev/null 2>&1 || fail "DocuSeal volume restore failed"

trap - EXIT
cleanup || fail "cannot clean up recovery staging"
printf 'restore ok %s\n' "$snapshot_id"
