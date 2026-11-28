#!/bin/bash
# Install the synthetic Linux test appliance after extracting its artifact bundle.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this guest-only provisioner as root." >&2
    exit 1
fi
if [[ ! -f /etc/debian_version ]] || ! command -v systemctl >/dev/null; then
    echo "This provisioner requires a booted Debian systemd guest." >&2
    exit 1
fi

app_root=/opt/biz-os
guest_python=/opt/biz-python/bin/python3.12
postgres_root=$app_root/.venv/lib/python3.12/site-packages/pgserver/pginstall
guest_files=$app_root/deploy/virtual_guest
for executable in "$guest_python" "$postgres_root/bin/postgres" /opt/biz-node/bin/node; do
    test -x "$executable"
done
test -f /opt/biz-web/server.js
test -f "$app_root/uv.lock"

if [[ ${1:-} != --skip-packages ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates libstdc++6 libgcc-s1 zlib1g libgomp1 libffi8 libssl3 \
        libbz2-1.0 liblzma5 libsqlite3-0 libreadline8 \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core
    apt-get clean
    rm -rf /var/lib/apt/lists/*
fi

getent group biz >/dev/null || groupadd --system biz
id biz >/dev/null 2>&1 || useradd --system --gid biz --home-dir /var/lib/biz-os \
    --shell /usr/sbin/nologin biz
getent group postgres >/dev/null || groupadd --system postgres
id postgres >/dev/null 2>&1 || useradd --system --gid postgres \
    --home-dir /var/lib/biz-os/postgres --shell /usr/sbin/nologin postgres

install -d -m 0755 /etc/biz-os /var/lib/biz-os
install -d -m 0700 -o postgres -g postgres /var/lib/biz-os/postgres
install -d -m 0700 -o biz -g biz /var/lib/biz-os/objects
chmod -R a+rX "$app_root" /opt/biz-python /opt/biz-web /opt/biz-node
if [[ ! -f /etc/biz-os/virtual.env ]]; then
    "$guest_python" - <<'PY'
import os
import secrets
from pathlib import Path

password = secrets.token_hex(24)
settings = {
    "BIZ_DATABASE_URL": f"postgresql+psycopg://biz:{password}@127.0.0.1:5432/biz",
    "BIZ_DBOS_SYSTEM_URL": f"postgresql+psycopg://biz:{password}@127.0.0.1:5432/biz_dbos",
    "BIZ_SECRET_KEY": secrets.token_hex(32),
    "BIZ_RP_ID": "localhost",
    "BIZ_ORIGIN": "http://localhost:3000",
    "BIZ_APP_DIR": "/opt/biz-os/apps/demo",
    "BIZ_EXECUTION_MODE": "shadow",
    "BIZ_REPO_ROOT": "/opt/biz-os",
    "BIZ_STORAGE_ROOT": "/var/lib/biz-os/objects",
    "BIZ_API_URL": "http://127.0.0.1:8000",
    "BIZ_TICK_SECONDS": "5",
    "BIZ_WORKFLOW_VERSION": "1",
    "BIZ_TRUSTED_PROXIES": "127.0.0.1/32",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "NEXT_TELEMETRY_DISABLED": "1",
    "NODE_ENV": "production",
    "HOME": "/var/lib/biz-os",
}
path = Path("/etc/biz-os/virtual.env")
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
with os.fdopen(fd, "w") as stream:
    stream.write("".join(f"{key}={value}\n" for key, value in settings.items()))
PY
    chown root:biz /etc/biz-os/virtual.env
fi

if [[ ! -f /var/lib/biz-os/postgres/PG_VERSION ]]; then
    runuser -u postgres -- "$postgres_root/bin/initdb" \
        -D /var/lib/biz-os/postgres --encoding=UTF8 --locale=C.UTF-8
    cat > /var/lib/biz-os/postgres/pg_hba.conf <<'HBA'
local all all peer
host all all 127.0.0.1/32 scram-sha-256
host all all ::1/128 scram-sha-256
HBA
    chown postgres:postgres /var/lib/biz-os/postgres/pg_hba.conf
fi

for unit in biz-postgres biz-api biz-worker biz-web biz-comparison; do
    install -m 0644 "$guest_files/$unit.service" "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable --now biz-postgres.service
for attempt in $(seq 1 60); do
    if runuser -u postgres -- "$postgres_root/bin/pg_isready" \
        -h /run/biz-postgres -U postgres >/dev/null; then
        break
    fi
    if [[ $attempt == 60 ]]; then
        journalctl -u biz-postgres.service --no-pager -n 40 >&2
        exit 1
    fi
    sleep 1
done

# The test database owner can create disposable databases, but is not a superuser.
# Install vector into template1 as the administrator so its scratch DBs inherit it.
runuser -u postgres -- "$postgres_root/bin/psql" -h /run/biz-postgres \
    -v ON_ERROR_STOP=1 -d template1 -c 'CREATE EXTENSION IF NOT EXISTS vector;'
password=$("$guest_python" - <<'PY'
from pathlib import Path
from urllib.parse import urlsplit
for line in Path("/etc/biz-os/virtual.env").read_text().splitlines():
    if line.startswith("BIZ_DATABASE_URL="):
        print(urlsplit(line.split("=", 1)[1]).password)
        break
PY
)
runuser -u postgres -- "$postgres_root/bin/psql" -h /run/biz-postgres \
    -v ON_ERROR_STOP=1 -v biz_password="$password" -d postgres <<'SQL'
SELECT 'CREATE ROLE biz LOGIN CREATEDB' WHERE NOT EXISTS (
    SELECT FROM pg_roles WHERE rolname = 'biz'
)\gexec
ALTER ROLE biz WITH PASSWORD :'biz_password';
SELECT 'CREATE DATABASE biz OWNER biz' WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'biz'
)\gexec
SELECT 'CREATE DATABASE biz_dbos OWNER biz' WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'biz_dbos'
)\gexec
SQL
unset password

"$app_root/.venv/bin/python" -c 'import ssl, psycopg, biz_kernel; print("Guest Python imports ready")'
systemctl enable --now biz-api.service biz-worker.service biz-web.service biz-comparison.service
echo 'Synthetic Linux test appliance provisioned; installed composition remains shadow with fake ports.'
