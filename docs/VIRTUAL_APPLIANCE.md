# Virtual BIZ OS test appliance

This is a bootable **Debian 12 x86_64 QCOW2 image**, running under QEMU TCG.
It is a software integration stand-in for the planned Mac mini. It does not emulate
macOS, Apple Silicon/Metal speed, FileVault, Keychain or physical power recovery.

The guest runs native systemd services rather than nested Docker:

- PostgreSQL 16 with pgvector and the application/DBOS databases.
- BIZ OS API, business worker, owner console and local comparison worker.
- CPU-only Ollama 0.40.19 with the pinned `smollm2:135m-instruct-q4_K_M` test model.

The installed business composition is `apps/demo`, with fake providers and shadow
mode. The small model exercises actual local inference and paired comparison;
it is not an approved HIVE model or evidence of business quality or time savings.
The ordinary packaged Docker stack remains separately covered by repository CI.

## Run an exported image

On the computer running QEMU, install `qemu-system-x86_64`, `qemu-img`,
`genisoimage` and OpenSSH client tools. The runner also supports `--tools-root`
for user-level extracted Debian tools. Keep the image manifest with the image.

```sh
python3 deploy/virtual_appliance.py --state ./vm create \
  --base ./biz-os-test-appliance.qcow2 \
  --base-sha512 SHA512_FROM_THE_IMAGE_MANIFEST
python3 deploy/virtual_appliance.py --state ./vm start
python3 deploy/virtual_appliance.py --state ./vm status
```

`create` requires an empty state directory and verifies the image hash before
creating a writable overlay. Defaults are 3 virtual CPUs, 6144 MiB RAM and a sparse
12 GiB disk. The SSH key and known-hosts file stay in the private state directory.
The exported image remains unchanged while the overlay runs.

The guest owner console is forwarded to host port **3000**. Its default passkey
origin is `http://localhost:3000` on the QEMU host. In a cloud workspace, use the
configured HTTP preview or an appropriate port forward; that host's localhost
is not the browser device's localhost. Configure the installation's origin and
relying-party ID when using a different HTTPS hostname.

SSH administration uses host loopback port 2222:

```sh
python3 deploy/virtual_appliance.py --state ./vm ssh -- uname -a
python3 deploy/virtual_appliance.py --state ./vm ssh -- systemctl is-active \
  biz-postgres biz-api biz-worker biz-web biz-comparison biz-ollama
```

Outbound guest networking is **disabled by default**. `start --network-enabled`
is an explicit build/download option. Local services, model inference and incoming
host forwards continue to work with outbound networking disabled.

## Build the guest

The pinned base URL and SHA-512 are in `deploy/virtual_appliance.py`. Download that
dated Debian cloud image, verify it through the runner and create/start a VM with
networking enabled for provisioning. The seed is a read-only virtio disk because
the small cloud kernel need not include IDE CD-ROM support.

Prepare the normal locked Python environment and production web build, then:

```sh
python deploy/virtual_guest/stage_bundle.py --source . --venv .venv \
  --web-source web --output /tmp/biz-os-bundle.tar.gz
python deploy/virtual_appliance.py --state ./vm upload \
  /tmp/biz-os-bundle.tar.gz /tmp/biz-os-bundle.tar.gz
python deploy/virtual_appliance.py --state ./vm ssh -- sh -c \
  'tar -xzf /tmp/biz-os-bundle.tar.gz -C / && rm /tmp/biz-os-bundle.tar.gz && bash /opt/biz-os/deploy/virtual_guest/provision.sh'
```

The bundle relocates the prepared environment to `/opt/biz-os/.venv`, a minimal
Python interpreter to `/opt/biz-python`, and Node/Next artifacts to `/opt/biz-node`
and `/opt/biz-web`. It excludes local environment files, caches and the host's
global Python packages. A matching `uv.lock` is required. Secrets are generated
inside the guest in `/etc/biz-os/virtual.env`; data lives in `/var/lib/biz-os`.

For builds behind an HTTP proxy, use the existing proxy and trusted CA for the
temporary guest APT download configuration. Remove that configuration and CA
before exporting; never embed build credentials into an image.

`deploy/virtual_model.py stage-runtime DIRECTORY` streams and checks the complete
official Ollama asset, retaining only the CPU runtime and licenses.
`stage-model DIRECTORY` verifies the pinned model manifest and every layer.
Copy those directories into the guest, then run its `install --runtime PATH
--models PATH` command. No GPU archive is retained. The model service listens only
on guest loopback and disables cloud routing.

## Acceptance and reboot proof

Inside the guest, run as root:

```sh
cd /opt/biz-os
.venv/bin/python -m deploy.virtual_acceptance --timeout 900 \
  --state-dir /var/lib/biz-os/acceptance-final \
  --out /var/lib/biz-os/acceptance-final/prepare.json
```

This must report `AWAITING_REBOOT`. It checks service/process configuration, HTTP
auth boundaries, PostgreSQL/vector/schema, PDF rendering, approval crash/replay,
native logical backup restoration and a second parked synthetic approval. That
second probe uses separate databases and fixture-authorized fake providers.

Perform a real VM shutdown/start or kernel reboot, keeping guest source unchanged,
then run:

```sh
cd /opt/biz-os
.venv/bin/python -m deploy.virtual_acceptance --verify-reboot --timeout 900 \
  --state-dir /var/lib/biz-os/acceptance-final \
  --out /var/lib/biz-os/acceptance-final/reboot.json
```

`PASS` requires a changed kernel boot ID, unchanged source, persistent database/file
markers and recovery/replay of the pending probe with exactly one fake send. Failed
or interrupted runs replace stale success with unverified evidence. Logical backup
inside this guest is not a claim of encrypted off-box disaster recovery.
The prepared image retains this evidence. After booting it, use the verification
phase for its existing checkpoint. For a new independent attempt, choose a fresh
`--state-dir`; preserve failed-attempt evidence instead of reusing its logs.

The model helper's `probe --output FILE` performs real local structured inference.
Its `paired --output FILE` uses genuine captured synthetic output, a correction,
version review, private tuning attestation, normal enqueue/execution and sealed
DBOS replay. It removes only its generated scratch databases afterward. A valid
`no_improvement` result demonstrates execution, not a model quality improvement.

## Export and resume

Shut down cleanly before converting the mutable disk:

```sh
python3 deploy/virtual_appliance.py --state ./vm stop
python3 deploy/virtual_appliance.py --state ./vm export ./biz-os-test-appliance.qcow2
```

Export refuses a running VM or an existing destination. It checks the converted
QCOW2 and writes SHA-256/SHA-512, size and resource metadata to its companion
manifest. Keep the image private: it contains the synthetic test databases and
the guest's test installation configuration. Boot a new overlay of that exported
image to preserve the downloadable artifact while continuing to test.
