"""Build and operate a disposable, bootable Linux surrogate for the BIZ OS appliance.

QEMU TCG deliberately does not claim macOS, Apple Silicon/Metal or hardware performance.
Host tools are ordinary QEMU/SSH/genisoimage; application services live in the guest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

BASE_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/20261117-2665/"
    "debian-12-genericcloud-amd64.qcow2"
)
BASE_SHA512 = (
    "946b917a37882f16e5b3b0b2cbe5744637d0cd36190b5b53cbb21d14a54557d5b45e"
    "9e523021b8f52c1707dbe470489f3515458e0680ded76ee2574b4a8daf1b"
)


def read_state(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "state.json").read_text())


def write_state(directory: Path, state: dict[str, Any]) -> None:
    target = directory / "state.json"
    temporary = directory / "state.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(target)


def tool(state: dict[str, Any], name: str) -> str:
    root = state.get("tools_root")
    candidate = Path(root) / "usr/bin" / name if root else None
    if candidate and candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"install {name}, or provide --tools-root with extracted host tools")
    return found


def tool_environment(state: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    if root := state.get("tools_root"):
        libraries = str(Path(root) / "usr/lib/x86_64-linux-gnu")
        env["LD_LIBRARY_PATH"] = libraries + (
            ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
        )
    return env


def alive(state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Do not mistake a reused PID or a zombie for this VM.
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        try:
            argv = [
                value.decode() for value in (proc / "cmdline").read_bytes().split(b"\0") if value
            ]
            if state.get("process_start") is not None:
                started = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
                if started != state["process_start"]:
                    return False
        except (FileNotFoundError, ProcessLookupError):
            return False
        return _is_vm_command(argv, state["name"])
    process = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return process.returncode == 0 and _is_vm_command(shlex.split(process.stdout), state["name"])


def _is_vm_command(argv: list[str], name: str) -> bool:
    if not argv or Path(argv[0]).name != "qemu-system-x86_64":
        return False
    try:
        return argv[argv.index("-name") + 1] == name
    except (ValueError, IndexError):
        return False


def create(directory: Path, args: argparse.Namespace) -> dict[str, Any]:
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("create requires an empty state directory; refusing existing files")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if (directory / "state.json").exists():
        raise RuntimeError("this state directory already owns a VM; use start/status")
    base = args.base.resolve()
    with base.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha512").hexdigest()
    if actual != args.base_sha512:
        raise RuntimeError("base image SHA-512 mismatch; refusing to boot unverified bytes")
    identifier = uuid.uuid4().hex[:12]
    state = {
        "name": f"biz-os-virtual-{identifier}",
        "base": str(base),
        "base_sha512": actual,
        "disk": str(directory / "system.qcow2"),
        "memory_mib": args.memory,
        "cpus": args.cpus,
        "ssh_port": args.ssh_port,
        "web_port": args.web_port,
        "api_port": args.api_port,
        "model_port": args.model_port,
        "tools_root": str(args.tools_root.resolve()) if args.tools_root else None,
        "network_enabled": False,
        "pid": None,
        "virtualization": "QEMU TCG x86_64, Linux; no macOS/Metal performance equivalence",
    }
    key = directory / "ssh_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    seed = directory / "seed"
    seed.mkdir(mode=0o700)
    config = {
        "hostname": "biz-os-virtual",
        "manage_etc_hosts": True,
        "disable_root": False,
        "ssh_pwauth": False,
        "users": [
            {
                "name": "root",
                "lock_passwd": True,
                "ssh_authorized_keys": [key.with_suffix(".pub").read_text().strip()],
            }
        ],
        "runcmd": [["touch", "/run/biz-cloud-init-ready"]],
    }
    (seed / "user-data").write_text("#cloud-config\n" + json.dumps(config, indent=2) + "\n")
    (seed / "meta-data").write_text(
        json.dumps({"instance-id": state["name"], "local-hostname": "biz-os-virtual"}) + "\n"
    )
    subprocess.run(
        [
            tool(state, "genisoimage"),
            "-quiet",
            "-output",
            str(directory / "seed.iso"),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            "user-data",
            "meta-data",
        ],
        cwd=seed,
        env=tool_environment(state),
        check=True,
    )
    subprocess.run(
        [
            tool(state, "qemu-img"),
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            state["disk"],
            args.disk_size,
        ],
        env=tool_environment(state),
        check=True,
    )
    write_state(directory, state)
    return state


def command(directory: Path, state: dict[str, Any], *, network: bool) -> list[str]:
    forwards = [
        f"hostfwd=tcp:127.0.0.1:{state['ssh_port']}-:22",
        f"hostfwd=tcp:0.0.0.0:{state['web_port']}-:3000",
        f"hostfwd=tcp:127.0.0.1:{state['api_port']}-:8000",
        f"hostfwd=tcp:127.0.0.1:{state['model_port']}-:11434",
    ]
    cmd = [
        tool(state, "qemu-system-x86_64"),
        "-name",
        state["name"],
        "-machine",
        "q35",
        "-accel",
        "tcg,thread=multi",
        "-cpu",
        "max",
        "-smp",
        str(state["cpus"]),
        "-m",
        str(state["memory_mib"]),
        "-display",
        "none",
        "-nodefaults",
        "-rtc",
        "base=utc",
        "-serial",
        f"file:{directory / 'serial.log'}",
        "-monitor",
        "none",
        "-qmp",
        f"unix:{directory / 'qmp.sock'},server=on,wait=off",
        "-drive",
        f"file={state['disk']},if=virtio,format=qcow2,discard=unmap,detect-zeroes=unmap",
        "-drive",
        f"file={directory / 'seed.iso'},if=virtio,format=raw,readonly=on",
        "-netdev",
        "user,id=net0,restrict=" + ("off" if network else "on") + "," + ",".join(forwards),
        "-device",
        "virtio-net-pci,netdev=net0,romfile=",
        "-object",
        "rng-random,filename=/dev/urandom,id=rng0",
        "-device",
        "virtio-rng-pci,rng=rng0",
    ]
    if root := state.get("tools_root"):
        cmd.extend(
            [
                "-L",
                str(Path(root) / "usr/share/qemu"),
                "-bios",
                str(Path(root) / "usr/share/seabios/bios-256k.bin"),
            ]
        )
    return cmd


def start(directory: Path, *, network: bool) -> dict[str, Any]:
    state = read_state(directory)
    if alive(state):
        raise RuntimeError("VM is already running; shut it down before changing network policy")
    with Path(state["base"]).open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha512").hexdigest()
    if checksum != state["base_sha512"]:
        raise RuntimeError("base image changed since verification; refusing to boot")
    for field in ("ssh_port", "web_port", "api_port", "model_port"):
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", state[field]))
    (directory / "qmp.sock").unlink(missing_ok=True)
    with (directory / "qemu.log").open("ab") as output:
        process = subprocess.Popen(
            command(directory, state, network=network),
            env=tool_environment(state),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    state.update(pid=process.pid, network_enabled=network)
    process_stat = Path(f"/proc/{process.pid}/stat")
    if process_stat.exists():
        state["process_start"] = process_stat.read_text().rsplit(")", 1)[1].split()[19]
    write_state(directory, state)
    time.sleep(0.3)
    if process.poll() is not None:
        raise RuntimeError("QEMU did not start; inspect qemu.log")
    return state


def ssh_command(directory: Path, state: dict[str, Any]) -> list[str]:
    return [
        "ssh",
        "-p",
        str(state["ssh_port"]),
        "-i",
        str(directory / "ssh_key"),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={directory / 'known_hosts'}",
        "root@127.0.0.1",
    ]


def stop(directory: Path) -> dict[str, Any]:
    state = read_state(directory)
    if alive(state):
        try:
            result = subprocess.run(
                ssh_command(directory, state) + ["systemctl poweroff"],
                check=False,
                timeout=8,
                capture_output=True,
            )
            requested = result.returncode == 0
        except subprocess.TimeoutExpired:
            requested = False
        if not requested and alive(state):
            # Also works before guest SSH/cloud-init is ready; never hard-kill a disk writer.
            try:
                with socket.socket(socket.AF_UNIX) as monitor:
                    monitor.settimeout(5)
                    monitor.connect(str(directory / "qmp.sock"))
                    wire = monitor.makefile("rwb", buffering=0)
                    wire.readline()
                    for request in ("qmp_capabilities", "system_powerdown"):
                        wire.write(json.dumps({"execute": request}).encode() + b"\n")
                        while True:
                            response = json.loads(wire.readline())
                            if "error" in response:
                                raise RuntimeError("QEMU refused graceful guest poweroff")
                            if "return" in response:
                                break
            except (OSError, ValueError):
                if alive(state):
                    raise
        deadline = time.monotonic() + 45
        while alive(state) and time.monotonic() < deadline:
            time.sleep(0.5)
        if alive(state):
            raise RuntimeError("guest did not shut down; refusing an unsafe live image export")
    state["pid"] = None
    write_state(directory, state)
    return state


def export(directory: Path, destination: Path) -> dict[str, Any]:
    state = read_state(directory)
    if alive(state):
        raise RuntimeError("power off the VM before exporting a consistent image")
    manifest_path = destination.with_suffix(".manifest.json")
    if destination.exists() or manifest_path.exists():
        raise RuntimeError("export destination exists; refusing to overwrite it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    if temporary.exists():
        raise RuntimeError("partial export exists; inspect it before retrying")
    subprocess.run(
        [tool(state, "qemu-img"), "convert", "-c", "-O", "qcow2", state["disk"], str(temporary)],
        env=tool_environment(state),
        check=True,
    )
    subprocess.run(
        [tool(state, "qemu-img"), "check", str(temporary)], env=tool_environment(state), check=True
    )
    temporary.chmod(0o600)
    # Another process may create the destination during a long conversion. Publish
    # without replacing it; an exists-check followed by replace would lose its data.
    os.link(temporary, destination)
    temporary.unlink()
    with destination.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    with destination.open("rb") as stream:
        sha512 = hashlib.file_digest(stream, "sha512").hexdigest()
    manifest = {
        "image": destination.name,
        "sha256": checksum,
        "sha512": sha512,
        "bytes": destination.stat().st_size,
        "architecture": "x86_64",
        "operating_system": "Debian GNU/Linux 12",
        "purpose": "private synthetic BIZ OS appliance test image",
        "macos_or_apple_silicon_equivalence": False,
        "source_vm": state["name"],
        "memory_mib": state["memory_mib"],
        "cpus": state["cpus"],
    }
    descriptor = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("create")
    init.add_argument("--base", type=Path, required=True)
    init.add_argument("--base-sha512", default=BASE_SHA512)
    init.add_argument("--tools-root", type=Path)
    init.add_argument("--memory", type=int, default=6144)
    init.add_argument("--cpus", type=int, default=3)
    init.add_argument("--disk-size", default="12G")
    for name, default in (("ssh", 2222), ("web", 3000), ("api", 8123), ("model", 11435)):
        init.add_argument(f"--{name}-port", type=int, default=default)
    boot = sub.add_parser("start")
    boot.add_argument("--network-enabled", action="store_true", help="Build/download phase only")
    sub.add_parser("status")
    sub.add_parser("stop")
    shell = sub.add_parser("ssh")
    shell.add_argument("args", nargs=argparse.REMAINDER)
    copy = sub.add_parser("upload")
    copy.add_argument("source", type=Path)
    copy.add_argument("destination")
    out = sub.add_parser("export")
    out.add_argument("destination", type=Path)
    args = parser.parse_args()
    directory = args.state.resolve()
    if args.action == "create":
        result = create(directory, args)
    elif args.action == "start":
        result = start(directory, network=args.network_enabled)
    elif args.action == "stop":
        result = stop(directory)
    elif args.action == "export":
        result = export(directory, args.destination.resolve())
    elif args.action == "status":
        result = read_state(directory)
        result["running"] = alive(result)
    elif args.action == "ssh":
        state = read_state(directory)
        command_args = args.args[1:] if args.args[:1] == ["--"] else args.args
        raise SystemExit(
            subprocess.call(ssh_command(directory, state) + [shlex.join(command_args)])
        )
    else:
        state = read_state(directory)
        destination = args.destination
        if not destination.startswith("/") or any(c.isspace() for c in destination):
            raise ValueError("upload needs an absolute guest path without whitespace")
        subprocess.run(
            [
                "scp",
                "-P",
                str(state["ssh_port"]),
                "-i",
                str(directory / "ssh_key"),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={directory / 'known_hosts'}",
                str(args.source.resolve()),
                f"root@127.0.0.1:{destination}",
            ],
            check=True,
        )
        return
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
