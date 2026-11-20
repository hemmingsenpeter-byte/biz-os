# Shared by backup.sh and restore.sh. Callers set HERE and enable strict Bash mode.
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

fail() {
  printf '%s: %s\n' "${0##*/}" "$*" >&2
  exit 1
}

ENV_FILE="${BIZ_ENV_FILE:-$HERE/../.env}"
COMPOSE_FILE="${BIZ_COMPOSE_FILE:-$HERE/../docker-compose.yml}"
# Compose gives the caller's environment priority over its env file.
HOST_ROOT_SET="${BIZ_STORAGE_HOST_ROOT+x}"
HOST_ROOT_OVERRIDE="${BIZ_STORAGE_HOST_ROOT:-}"
# The operator-owned environment file also supplies restic credentials.
set -a
. "$ENV_FILE" >/dev/null 2>&1 || fail "cannot load the environment file"
set +a
if [[ -n "$HOST_ROOT_SET" ]]; then
  export BIZ_STORAGE_HOST_ROOT="$HOST_ROOT_OVERRIDE"
fi

DUMP_DIR="${BIZ_DUMP_DIR:-$HOME/biz-backups/dumps}"
HOST_OBJECTS="${BIZ_STORAGE_HOST_ROOT:-${BIZ_STORAGE_ROOT:-}}"
[[ "$HOST_OBJECTS" == /* && -d "$HOST_OBJECTS" ]] ||
  fail "BIZ_STORAGE_HOST_ROOT must name an existing absolute directory"
[[ -n "${RESTIC_REPOSITORY:-}" ]] || fail "RESTIC_REPOSITORY is required"
mkdir -p -- "$DUMP_DIR" || fail "cannot create the backup working directory"
DUMP_DIR="$(cd "$DUMP_DIR" && pwd -P)"
STORAGE="$(cd "$HOST_OBJECTS" && pwd -P)"
# Quiescing protects consistency only if the archive cannot pick up our own writes.
[[ "$STORAGE" != / && "$DUMP_DIR" != / && "$DUMP_DIR/" != "$STORAGE/"* &&
   "$STORAGE/" != "$DUMP_DIR/"* ]] || fail "backup and object directories must be disjoint"

DBS=(biz biz_dbos docuseal)
FILES=(biz.dump biz_dbos.dump docuseal.dump objects.tar docuseal.tar)
LOCK="$DUMP_DIR/.lock"
TEMP=
STAGE=
mkdir -- "$LOCK" 2>/dev/null || fail "backup/restore lock is held or unavailable"
cleanup() {
  local result=0
  if [[ -n "$TEMP" ]]; then rm -rf -- "$TEMP" || result=1; fi
  if [[ -n "$STAGE" ]]; then rm -rf -- "$STAGE" || result=1; fi
  rmdir -- "$LOCK" || result=1
  return "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

dc() {
  local files=(-f "$COMPOSE_FILE")
  if [[ -n "${BIZ_COMPOSE_OVERRIDE_FILE:-}" ]]; then
    files+=(-f "$BIZ_COMPOSE_OVERRIDE_FILE")
  fi
  docker compose "${files[@]}" --env-file "$ENV_FILE" "$@"
}

require_stopped_writers() {
  local active
  active="$(dc ps --all --status running --status restarting --status paused -q \
    biz-api biz-worker docuseal 2>/dev/null)" || fail "cannot check Compose writer status"
  [[ -z "$active" ]] || fail "stop biz-api, biz-worker and docuseal before proceeding"
}

object_mount_hint() {
  printf '%s' "set BIZ_STORAGE_HOST_ROOT in the environment file to the directory the" \
    " API and worker mount (recreate the containers if they use another) before proceeding"
}

# Prints the host source Compose would bind at the object path for biz-api and biz-worker.
configured_object_mount() {
  dc config --format json 2>/dev/null | python3 -I -c '
import json, sys
services = json.load(sys.stdin).get("services") or {}
sources = set()
for name in ("biz-api", "biz-worker"):
    mounts = [
        volume for volume in (services.get(name) or {}).get("volumes") or []
        if isinstance(volume, dict) and volume.get("type") == "bind"
        and volume.get("target") == "/var/lib/biz-os/objects"
    ]
    if len(mounts) != 1:
        sys.exit(1)
    sources.add(str(mounts[0].get("source") or ""))
if len(sources) != 1:
    sys.exit(1)
print(sources.pop())
'
}

require_same_objects() {
  local label="$1" source="$2" canonical
  canonical="$(cd "$source" 2>/dev/null && pwd -P)" || canonical="$source"
  [[ "$canonical" == "$STORAGE" ]] ||
    fail "$label object mount differs from $HOST_OBJECTS (it is $canonical);" \
      "$(object_mount_hint)"
}

# Fail closed unless the archived directory is the one the API and worker mount, both as
# Compose would create them now and as any existing containers were created. A legacy
# .env with only BIZ_STORAGE_ROOT may name a directory Compose no longer mounts.
verify_installed_object_mounts() {
  local configured ids id mount
  configured="$(configured_object_mount)" ||
    fail "cannot resolve one Compose bind mount for biz-api and biz-worker objects;" \
      "$(object_mount_hint)"
  [[ "$configured" == /* ]] || fail "Compose object mount must be an absolute host path"
  require_same_objects "Compose" "$configured"
  ids="$(dc ps -a -q biz-api biz-worker 2>/dev/null)" ||
    fail "cannot check the installed object mounts"
  [[ -n "$ids" ]] || return 0  # clean recovery may have no application containers
  while IFS= read -r id; do
    [[ "$id" =~ ^[0-9a-f]+$ ]] || fail "invalid application container ID"
    mount="$(docker inspect --format \
      '{{range .Mounts}}{{if and (eq .Destination "/var/lib/biz-os/objects") (eq .Type "bind")}}{{.Source}}{{end}}{{end}}' \
      "$id" 2>/dev/null)" || fail "cannot inspect the installed object mount"
    if [[ "$OSTYPE" == darwin* ]]; then
      mount="${mount#/host_mnt}"
    fi
    [[ "$mount" == /* ]] || fail "installed application lacks a bind mount for its objects"
    require_same_objects "installed application" "$mount"
  done <<<"$ids"
}

find_docuseal_volume() {
  local docuseal_id postgres_id
  docuseal_id="$(dc ps -a -q docuseal 2>/dev/null)" ||
    fail "cannot find the DocuSeal container"
  postgres_id="$(dc ps -q postgres 2>/dev/null)" ||
    fail "cannot find the PostgreSQL container"
  [[ "$docuseal_id" =~ ^[0-9a-f]+$ && "$postgres_id" =~ ^[0-9a-f]+$ ]] ||
    fail "expected one created DocuSeal container and one running PostgreSQL container"

  if [[ -n "${BIZ_BACKUP_HELPER_IMAGE:-}" ]]; then
    IMAGE="$(docker image inspect --format '{{.Id}}' "$BIZ_BACKUP_HELPER_IMAGE" 2>/dev/null)" ||
      fail "cannot find the configured local archive helper image"
  else
    IMAGE="$(docker inspect --format '{{.Image}}' "$postgres_id" 2>/dev/null)" ||
      fail "cannot determine the local archive helper image"
  fi
  VOLUME="$(docker inspect --format \
    '{{range .Mounts}}{{if and (eq .Destination "/data/docuseal") (eq .Type "volume")}}{{.Name}}{{end}}{{end}}' \
    "$docuseal_id" 2>/dev/null)" || fail "cannot inspect the DocuSeal container"
  [[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ && "$VOLUME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] ||
    fail "expected a local image ID and a named DocuSeal volume at /data/docuseal"
}

manifest_for() {
  printf 'BIZ_STORAGE_ROOT=%s\n' "$STORAGE"
  (cd "$1" && shasum -a 256 "${FILES[@]}")
}
