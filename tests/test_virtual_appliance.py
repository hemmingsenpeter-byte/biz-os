"""Host lifecycle safeguards use fake commands; no test touches a running appliance."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from deploy import virtual_appliance as vm
from deploy.virtual_guest import stage_bundle


@pytest.fixture
def host(tmp_path: Path, monkeypatch):
    commands = []

    def run(arguments, **kwargs):
        command = [str(item) for item in arguments]
        commands.append(command)
        if command[0] == "ssh-keygen":
            key = Path(command[command.index("-f") + 1])
            key.write_text("synthetic private key")
            key.with_suffix(".pub").write_text("ssh-ed25519 synthetic-key\n")
        elif command[0].endswith("genisoimage"):
            Path(command[command.index("-output") + 1]).write_bytes(b"synthetic seed")
        elif command[0].endswith("qemu-img") and command[1] == "create":
            Path(command[-2]).write_bytes(b"synthetic overlay")
        elif command[0].endswith("qemu-img") and command[1] == "convert":
            Path(command[-1]).write_bytes(b"synthetic standalone disk")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vm, "tool", lambda state, name: "/fake/" + name)
    monkeypatch.setattr(vm.subprocess, "run", run)
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"synthetic verified base")
    args = argparse.Namespace(
        base=base,
        base_sha512=hashlib.sha512(base.read_bytes()).hexdigest(),
        memory=2048,
        cpus=2,
        ssh_port=2222,
        web_port=3000,
        api_port=8123,
        model_port=11435,
        tools_root=None,
        disk_size="8G",
    )
    return SimpleNamespace(directory=tmp_path / "vm", base=base, args=args, commands=commands)


def created(host):
    state = vm.create(host.directory, host.args)
    host.commands.clear()
    return state


class PortSocket:
    def __init__(self, binds, occupied=None):
        self.binds, self.occupied = binds, occupied

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def bind(self, address):
        self.binds.append(address)
        if address[1] == self.occupied:
            raise OSError("address already in use")


def fake_start(monkeypatch, *, occupied=None, returncode=None):
    binds: list[tuple[str, int]] = []
    process = SimpleNamespace(pid=43210, poll=lambda: returncode)
    popen = Mock(return_value=process)
    monkeypatch.setattr(vm, "alive", lambda state: False)
    monkeypatch.setattr(vm.socket, "socket", lambda *args, **kwargs: PortSocket(binds, occupied))
    monkeypatch.setattr(vm.subprocess, "Popen", popen)
    monkeypatch.setattr(vm.time, "sleep", lambda seconds: None)
    return binds, popen


def test_create_records_verified_base_and_private_offline_state(host):
    state = vm.create(host.directory, host.args)
    assert state["base_sha512"] == host.args.base_sha512
    assert state["network_enabled"] is False and state["pid"] is None
    assert host.directory.stat().st_mode & 0o777 == 0o700
    assert (host.directory / "state.json").stat().st_mode & 0o777 == 0o600
    assert "no macOS/Metal" in state["virtualization"]
    assert (host.directory / "seed.iso").is_file()


def test_checksum_is_checked_before_any_image_or_key_command(host):
    host.args.base_sha512 = "0" * 128
    with pytest.raises(RuntimeError, match="SHA|checksum|unverified"):
        vm.create(host.directory, host.args)
    assert host.commands == []
    assert not (host.directory / "system.qcow2").exists()
    assert not (host.directory / "state.json").exists()


def test_start_refuses_base_changed_since_creation(host, monkeypatch):
    created(host)
    host.base.write_bytes(b"changed after checksum verification")
    _, popen = fake_start(monkeypatch)
    with pytest.raises(RuntimeError, match="SHA|checksum|unverified|verification"):
        vm.start(host.directory, network=False)
    popen.assert_not_called()


def test_existing_state_is_never_recreated(host):
    created(host)
    before = (host.directory / "state.json").read_bytes()
    with pytest.raises(RuntimeError, match="already|exist|nonempty"):
        vm.create(host.directory, host.args)
    assert (host.directory / "state.json").read_bytes() == before
    assert host.commands == []


def test_unregistered_existing_disk_cannot_be_overwritten(host):
    host.directory.mkdir()
    disk = host.directory / "system.qcow2"
    disk.write_bytes(b"existing appliance disk: preserve")
    with pytest.raises(RuntimeError, match="already|exist|nonempty"):
        vm.create(host.directory, host.args)
    assert disk.read_bytes() == b"existing appliance disk: preserve"
    assert host.commands == []


@pytest.mark.parametrize("network,restriction", [(False, "on"), (True, "off")])
def test_qemu_network_has_only_explicit_forwards_and_tcg(host, network, restriction):
    state = created(host)
    command = vm.command(host.directory, state, network=network)
    network_argument = command[command.index("-netdev") + 1]
    assert f"restrict={restriction}" in network_argument
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in network_argument
    assert "hostfwd=tcp:127.0.0.1:8123-:8000" in network_argument
    assert "hostfwd=tcp:127.0.0.1:11435-:11434" in network_argument
    assert "hostfwd=tcp:0.0.0.0:3000-:3000" in network_argument
    assert network_argument.count("hostfwd=") == 4
    assert command[command.index("-accel") + 1] == "tcg,thread=multi"
    assert "-enable-kvm" not in command
    assert any("if=virtio,format=raw,readonly=on" in argument for argument in command)


def test_start_persists_policy_and_detaches_only_after_port_checks(host, monkeypatch):
    created(host)
    binds, popen = fake_start(monkeypatch)
    state = vm.start(host.directory, network=False)
    assert len(binds) == 4
    assert state["pid"] == 43210 and state["network_enabled"] is False
    assert vm.read_state(host.directory) == state
    assert popen.call_args.kwargs["start_new_session"] is True
    assert "restrict=on" in popen.call_args.args[0][popen.call_args.args[0].index("-netdev") + 1]


@pytest.mark.parametrize("flag,network", [([], False), (["--network-enabled"], True)])
def test_cli_requires_explicit_outbound_network_opt_in(host, monkeypatch, capsys, flag, network):
    start = Mock(return_value={"network_enabled": network})
    monkeypatch.setattr(vm, "start", start)
    monkeypatch.setattr(
        sys, "argv", ["virtual_appliance", "--state", str(host.directory), "start", *flag]
    )
    vm.main()
    start.assert_called_once_with(host.directory.resolve(), network=network)
    assert json.loads(capsys.readouterr().out)["network_enabled"] is network


def test_port_collision_refuses_before_start_and_preserves_state(host, monkeypatch):
    created(host)
    before = (host.directory / "state.json").read_bytes()
    _, popen = fake_start(monkeypatch, occupied=3000)
    with pytest.raises(OSError, match="already in use"):
        vm.start(host.directory, network=False)
    assert (host.directory / "state.json").read_bytes() == before
    popen.assert_not_called()


def test_early_qemu_exit_is_reported_as_failure(host, monkeypatch):
    created(host)
    _, popen = fake_start(monkeypatch, returncode=1)
    with pytest.raises(RuntimeError, match="QEMU|qemu.log"):
        vm.start(host.directory, network=False)
    popen.assert_called_once()
    assert (host.directory / "qemu.log").exists()


@pytest.mark.parametrize(
    "command,expected",
    [
        (b"/fake/qemu-system-x86_64\0-name\0biz-os-virtual-fixture\0", True),
        (b"/usr/bin/python3\0unrelated-job\0", False),
        (b"/usr/bin/python3\0biz-os-virtual-fixture\0", False),
        (b"", False),
    ],
)
def test_liveness_does_not_signal_or_accept_reused_processes(monkeypatch, command, expected):
    kill = Mock()
    monkeypatch.setattr(vm.os, "kill", kill)
    real_path = Path

    class Proc:
        def exists(self):
            return True

        def __truediv__(self, field):
            return SimpleNamespace(read_bytes=lambda: command)

    monkeypatch.setattr(
        vm, "Path", lambda value: Proc() if str(value) == "/proc/43210" else real_path(value)
    )
    assert vm.alive({"pid": 43210, "name": "biz-os-virtual-fixture"}) is expected
    assert kill.call_args_list == [((43210, 0),)]


@pytest.mark.parametrize("recorded_start,expected", [("111", False), ("222", True)])
def test_liveness_checks_process_birth_even_for_the_same_qemu_name(
    monkeypatch, recorded_start, expected
):
    monkeypatch.setattr(vm.os, "kill", Mock())
    real_path = Path
    command = b"/fake/qemu-system-x86_64\0-name\0biz-os-virtual-fixture\0"
    process_stat = "43210 (qemu-system-x86_64) S " + "0 " * 18 + "222 0"

    class Proc:
        def exists(self):
            return True

        def __truediv__(self, field):
            return SimpleNamespace(read_bytes=lambda: command, read_text=lambda: process_stat)

    monkeypatch.setattr(
        vm, "Path", lambda value: Proc() if str(value) == "/proc/43210" else real_path(value)
    )
    assert (
        vm.alive({"pid": 43210, "name": "biz-os-virtual-fixture", "process_start": recorded_start})
        is expected
    )


def test_stop_uses_ssh_and_never_hard_kills(host, monkeypatch):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", Mock(side_effect=[True, False, False]))
    kill = Mock(side_effect=AssertionError("must not kill a disk writer"))
    monkeypatch.setattr(vm.os, "kill", kill)
    result = vm.stop(host.directory)
    assert result["pid"] is None
    assert host.commands[0][-1] == "systemctl poweroff"
    kill.assert_not_called()


def test_guest_already_off_after_ssh_disconnect_needs_no_qmp(host, monkeypatch):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", Mock(side_effect=[True, False, False, False]))
    monkeypatch.setattr(vm.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=255)))
    monitor = MagicMock()
    monitor.__enter__.return_value = monitor
    monitor.connect.side_effect = FileNotFoundError("QEMU socket disappeared after clean poweroff")
    monkeypatch.setattr(vm.socket, "socket", Mock(return_value=monitor))
    assert vm.stop(host.directory)["pid"] is None
    assert vm.read_state(host.directory)["pid"] is None


class Monitor:
    def __init__(self, responses):
        self.responses, self.writes, self.connected = deque(responses), [], None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def settimeout(self, seconds):
        assert seconds > 0

    def connect(self, path):
        self.connected = path

    def makefile(self, *args, **kwargs):
        return self

    def readline(self):
        return self.responses.popleft()

    def write(self, content):
        self.writes.append(json.loads(content))


@pytest.mark.parametrize("ssh_timeout", [False, True])
def test_ssh_failure_uses_graceful_qmp_powerdown(host, monkeypatch, ssh_timeout):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", Mock(side_effect=[True, True, False, False]))
    monkeypatch.setattr(vm.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(vm.time, "monotonic", lambda: 0)
    if ssh_timeout:
        monkeypatch.setattr(
            vm.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("ssh", 8))
        )
    else:
        monkeypatch.setattr(
            vm.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=255))
        )
    monitor = Monitor(
        [b'{"QMP":{}}\n', b'{"event":"RESUME"}\n', b'{"return":{}}\n', b'{"return":{}}\n']
    )
    monkeypatch.setattr(vm.socket, "socket", lambda *args: monitor)
    assert vm.stop(host.directory)["pid"] is None
    assert monitor.connected == str(host.directory / "qmp.sock")
    assert monitor.writes == [{"execute": "qmp_capabilities"}, {"execute": "system_powerdown"}]


def test_shutdown_timeout_preserves_live_state_and_refuses_force(host, monkeypatch):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", lambda state: True)
    clock = iter([0, 46])
    monkeypatch.setattr(vm.time, "monotonic", lambda: next(clock))
    kill = Mock(side_effect=AssertionError("unsafe signal"))
    monkeypatch.setattr(vm.os, "kill", kill)
    with pytest.raises(RuntimeError, match="shut down|unsafe"):
        vm.stop(host.directory)
    assert vm.read_state(host.directory)["pid"] == 43210
    kill.assert_not_called()


def test_qmp_refusal_keeps_the_vm_registered_as_live(host, monkeypatch):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", lambda state: True)
    monkeypatch.setattr(vm.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=255)))
    monitor = Monitor([b'{"QMP":{}}\n', b'{"error":{"class":"GenericError"}}\n'])
    monkeypatch.setattr(vm.socket, "socket", lambda *args: monitor)
    with pytest.raises(RuntimeError, match="refused|poweroff"):
        vm.stop(host.directory)
    assert vm.read_state(host.directory)["pid"] == 43210


def test_qmp_eof_after_guest_poweroff_is_a_successful_stop(host, monkeypatch):
    state = created(host)
    state["pid"] = 43210
    vm.write_state(host.directory, state)
    monkeypatch.setattr(vm, "alive", Mock(side_effect=[True, True, False, False, False]))
    monkeypatch.setattr(vm.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=255)))
    monitor = Monitor([b'{"QMP":{}}\n', b""])
    monkeypatch.setattr(vm.socket, "socket", lambda *args: monitor)
    assert vm.stop(host.directory)["pid"] is None
    assert vm.read_state(host.directory)["pid"] is None


def test_export_refuses_a_live_vm_before_image_commands(host, monkeypatch):
    created(host)
    monkeypatch.setattr(vm, "alive", lambda state: True)
    with pytest.raises(RuntimeError, match="power off|consistent"):
        vm.export(host.directory, host.directory.parent / "export.qcow2")
    assert host.commands == []


@pytest.mark.parametrize("existing", ["image", "partial", "manifest"])
def test_export_preserves_existing_image_or_partial(host, monkeypatch, existing):
    created(host)
    monkeypatch.setattr(vm, "alive", lambda state: False)
    output = host.directory.parent / "export.qcow2"
    reserved = {
        "image": output,
        "partial": output.with_suffix(".qcow2.partial"),
        "manifest": output.with_suffix(".manifest.json"),
    }[existing]
    reserved.write_bytes(b"existing content: preserve")
    with pytest.raises(RuntimeError, match="exist|overwrite"):
        vm.export(host.directory, output)
    assert reserved.read_bytes() == b"existing content: preserve"
    assert host.commands == []


def test_export_checks_standalone_image_and_binds_manifest_to_bytes(host, monkeypatch):
    created(host)
    monkeypatch.setattr(vm, "alive", lambda state: False)
    output = host.directory.parent / "export.qcow2"
    manifest = vm.export(host.directory, output)
    assert manifest["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["bytes"] == len(output.read_bytes())
    assert manifest["macos_or_apple_silicon_equivalence"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.with_suffix(".manifest.json").stat().st_mode & 0o777 == 0o600
    assert [command[1] for command in host.commands] == ["convert", "check"]
    assert "-b" not in host.commands[0]
    assert not output.with_suffix(".qcow2.partial").exists()


def test_export_does_not_overwrite_destination_created_during_conversion(host, monkeypatch):
    created(host)
    monkeypatch.setattr(vm, "alive", lambda state: False)
    output = host.directory.parent / "export.qcow2"
    run = vm.subprocess.run

    def competing_export(arguments, **kwargs):
        result = run(arguments, **kwargs)
        if "convert" in arguments:
            output.write_bytes(b"another saved image: preserve")
        return result

    monkeypatch.setattr(vm.subprocess, "run", competing_export)
    with pytest.raises((RuntimeError, FileExistsError), match="exist|overwrite"):
        vm.export(host.directory, output)
    assert output.read_bytes() == b"another saved image: preserve"


def test_failed_image_check_does_not_publish_an_export(host, monkeypatch):
    created(host)
    monkeypatch.setattr(vm, "alive", lambda state: False)
    output = host.directory.parent / "export.qcow2"
    run = vm.subprocess.run

    def refused_image(arguments, **kwargs):
        if "check" in arguments:
            raise subprocess.CalledProcessError(1, arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(vm.subprocess, "run", refused_image)
    with pytest.raises(subprocess.CalledProcessError):
        vm.export(host.directory, output)
    assert not output.exists()
    assert not output.with_suffix(".manifest.json").exists()
    assert output.with_suffix(".qcow2.partial").is_file()


def test_source_entries_omit_secrets_and_caches_but_keep_templates(tmp_path):
    source = tmp_path / "apps"
    source.mkdir()
    for path in (
        ".env",
        ".env.local",
        "hive/.env.production",
        "hive/__pycache__/cached.pyc",
        "hive/.git/config",
        "hive/node_modules/dependency",
        "hive/.env.example",
        "hive/composition.yaml",
    ):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic fixture")
    included = {
        path.relative_to(source).as_posix()
        for path in stage_bundle.entries(source, stage_bundle.EXCLUDED)
    }
    assert "hive/.env.example" in included and "hive/composition.yaml" in included
    assert not any(
        path.endswith((".env", ".env.local", ".env.production", ".pyc", "dependency", "config"))
        for path in included
    )
