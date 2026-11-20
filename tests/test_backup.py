"""Exercise the backup entry point with local fakes and disposable files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

BACKUP = Path(__file__).resolve().parents[1] / "deploy" / "backups" / "backup.sh"
SNAPSHOT_ID = "a" * 64

FAKE_TOOLS = """
import json
import os
import shutil
import sys
from pathlib import Path

tool, *args = sys.argv[1:]
with Path(os.environ["BIZ_TEST_COMMANDS"]).open("a") as log:
    print(json.dumps([tool, *args]), file=log)

def fail(operation):
    if operation == os.environ.get("BIZ_TEST_FAILURE"):
        print(os.environ["RESTIC_PASSWORD"], file=sys.stderr)
        sys.exit(1)

if tool == "docker":
    if "pg_dump" in args:
        fail("dump:" + args[-1])
        print("fake dump for", args[-1])
    elif "pg_restore" in args:
        assert sys.stdin.read().startswith("fake dump for")
        fail("pg_restore")
    elif "ps" in args:
        if "--status" in args:
            if os.environ.get("BIZ_TEST_ACTIVE"):
                print("active-writer")
        elif args[-2:] == ["biz-api", "biz-worker"]:
            if os.environ.get("BIZ_TEST_INSTALLED_ROOT"):
                print("e" * 12)
        elif args[-1] in {"postgres", "docuseal"}:
            print("b" * 12 if args[-1] == "postgres" else "d" * 12)
    elif args[0] == "inspect":
        if args[-1] == "e" * 12:
            print(os.environ["BIZ_TEST_INSTALLED_ROOT"])
        else:
            print("sha256:" + "b" * 64 if args[-1] == "b" * 12 else "docuseal-volume")
    elif args[0] == "run":
        fail("docuseal-archive")
        sys.stdout.buffer.write(Path(os.environ["BIZ_TEST_DOCUSEAL_ARCHIVE"]).read_bytes())
    else:
        sys.exit("unexpected docker invocation")
elif tool == "restic":
    command = args[0]
    fail(command)
    fixture = Path(os.environ["BIZ_TEST_FIXTURE"])
    snapshot = fixture / "remote-snapshot"
    if command == "backup" and "--json" in args:
        source = Path(args[-1])
        (fixture / "snapshot-source").write_text(str(source))
        shutil.copytree(source, snapshot)
        print(json.dumps({"message_type": "summary", "snapshot_id": "a" * 64}))
    elif command == "restore":
        assert args[1] == "a" * 64
        target = Path(args[args.index("--target") + 1])
        source = Path((fixture / "snapshot-source").read_text())
        restored_stage = target.joinpath(*source.parts[1:])
        shutil.copytree(snapshot, restored_stage)
        if os.environ.get("BIZ_TEST_CORRUPT"):
            (restored_stage / "biz_dbos.dump").write_text("corrupt backup")
    elif command not in {"backup", "cat", "check", "forget", "init", "snapshots"}:
        sys.exit("unexpected restic invocation")
"""

# Exported Bash functions shadow the launchd PATH prefix in the production entry point.
LAUNCHER = """
docker() { "$BIZ_TEST_PYTHON" "$BIZ_TEST_FAKE" docker "$@"; }
restic() { "$BIZ_TEST_PYTHON" "$BIZ_TEST_FAKE" restic "$@"; }
rm() {
    if [[ "${BIZ_TEST_FAILURE:-}" == cleanup && "${!#}" == */stage ]]; then
        return 1
    fi
    command rm "$@"
}
export -f docker restic rm
exec bash "$1"
"""


@dataclass
class BackupRun:
    result: subprocess.CompletedProcess[str]
    commands: list[list[str]]
    known_good: Path
    snapshot: Path

    def restic_calls(self) -> list[list[str]]:
        return [args[1:] for args in self.commands if args[0] == "restic"]


@pytest.fixture
def run_backup(tmp_path: Path) -> Callable[..., BackupRun]:
    def run(**overrides: str) -> BackupRun:
        config = tmp_path / "backup.env"
        config.write_text(
            "RESTIC_REPOSITORY=disposable\n"
            "RESTIC_PASSWORD=disposable-secret\n"
            "BIZ_SECRET_KEY=disposable-app-secret\n"
        )
        document_root = tmp_path / "documents"
        document_root.mkdir()
        (document_root / "saved.txt").write_text("synthetic local content\n")
        if overrides.get("BIZ_TEST_USE_HOST_ROOT"):
            obsolete = tmp_path / "obsolete"
            obsolete.mkdir()
            with config.open("a") as env_file:
                env_file.write(f"BIZ_STORAGE_HOST_ROOT={obsolete}\n")
            overrides["BIZ_STORAGE_HOST_ROOT"] = str(document_root)
            installed = obsolete if overrides.get("BIZ_TEST_MOUNT_DRIFT") else document_root
            overrides["BIZ_TEST_INSTALLED_ROOT"] = str(installed)
        docuseal_root = tmp_path / "docuseal"
        docuseal_root.mkdir()
        (docuseal_root / "signed.pdf").write_text("synthetic signature file\n")
        docuseal_archive = tmp_path / "fake-docuseal.tar"
        with tarfile.open(docuseal_archive, "w") as archive:
            archive.add(docuseal_root, arcname=".")
        dump_root = tmp_path / "dumps"
        dump_root.mkdir()
        known_good = dump_root / "biz-known-good.dump"
        known_good.write_text("previous verified backup")
        old_timestamp = time.time() - 10 * 24 * 60 * 60
        os.utime(known_good, (old_timestamp, old_timestamp))
        fake_tools = tmp_path / "fake-tools.py"
        fake_tools.write_text(FAKE_TOOLS)
        log = tmp_path / "commands.jsonl"
        result = subprocess.run(
            ["bash", "-c", LAUNCHER, "backup-test", str(BACKUP)],
            env={
                "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
                "HOME": str(tmp_path),
                "BIZ_ENV_FILE": str(config),
                "BIZ_DUMP_DIR": str(dump_root),
                "BIZ_STORAGE_ROOT": str(document_root),
                "BIZ_TEST_COMMANDS": str(log),
                "BIZ_TEST_PYTHON": sys.executable,
                "BIZ_TEST_FAKE": str(fake_tools),
                "BIZ_TEST_FIXTURE": str(tmp_path),
                "BIZ_TEST_DOCUSEAL_ARCHIVE": str(docuseal_archive),
                **overrides,
            },
            text=True,
            capture_output=True,
            timeout=30,
        )
        commands = (
            [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        )
        return BackupRun(result, commands, known_good, tmp_path / "remote-snapshot")

    return run


def test_dumps_the_workflow_database(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup()
    assert backup.result.returncode == 0, backup.result.stderr
    assert {args[-1] for args in backup.commands if "pg_dump" in args} == {
        "biz",
        "biz_dbos",
        "docuseal",
    }


def test_failed_dump_prevents_backup_success(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup(BIZ_TEST_FAILURE="dump:biz")
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert all(args[0] != "backup" for args in backup.restic_calls())
    assert backup.known_good.read_text() == "previous verified backup"


def test_verified_snapshot_contains_the_recovery_boundary(
    run_backup: Callable[..., BackupRun],
) -> None:
    backup = run_backup()
    assert backup.result.returncode == 0, backup.result.stderr
    assert backup.result.stdout.strip() == f"backup ok {SNAPSHOT_ID}"
    calls = backup.restic_calls()
    assert [args[0] for args in calls] == [
        "snapshots",
        "backup",
        "restore",
        "check",
        "forget",
        "cat",
    ]
    assert "--verify" in calls[2]
    assert calls[2][1] == SNAPSHOT_ID
    assert "--tag" in calls[4] and "biz-nightly" in calls[4]
    assert "--keep-last" in calls[4]
    tag = calls[4][calls[4].index("--keep-tag") + 1]
    assert calls[1][calls[1].index("--tag") + 1] == f"biz-nightly,{tag}"
    assert "--prune" not in calls[4]
    assert calls[5] == ["cat", "snapshot", SNAPSHOT_ID]

    dumps = {path.name: path.read_text().strip() for path in backup.snapshot.rglob("*.dump")}
    assert dumps == {f"{db}.dump": f"fake dump for {db}" for db in ("biz", "biz_dbos", "docuseal")}
    recovered_files: dict[str, bytes] = {}
    for path in backup.snapshot.rglob("*.tar"):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    recovered_files[Path(member.name).name] = extracted.read()
    assert recovered_files["saved.txt"] == b"synthetic local content\n"
    assert recovered_files["signed.pdf"] == b"synthetic signature file\n"
    for path in backup.snapshot.rglob("*"):
        if path.is_file():
            assert b"disposable-app-secret" not in path.read_bytes()


@pytest.mark.parametrize(
    ("phase", "last_restic_command"),
    [
        ("pg_restore", "snapshots"),
        ("docuseal-archive", "snapshots"),
        ("snapshots", "snapshots"),
        ("backup", "backup"),
        ("restore", "restore"),
        ("check", "check"),
        ("forget", "forget"),
        ("cat", "cat"),
    ],
)
def test_failures_never_report_success_or_remove_known_good_data(
    run_backup: Callable[..., BackupRun],
    phase: str,
    last_restic_command: str,
) -> None:
    backup = run_backup(BIZ_TEST_FAILURE=phase)
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert backup.result.stderr
    assert "disposable-secret" not in backup.result.stderr + backup.result.stdout
    assert all(args[0] not in {"init", "prune"} for args in backup.restic_calls())
    # Local archive checks may run before or after the repository access check.
    if last_restic_command == "snapshots":
        assert all(args[0] == "snapshots" for args in backup.restic_calls())
    else:
        assert backup.restic_calls()[-1][0] == last_restic_command
    assert backup.known_good.read_text() == "previous verified backup"


def test_stop_before_retention_when_restored_bytes_differ(
    run_backup: Callable[..., BackupRun],
) -> None:
    backup = run_backup(BIZ_TEST_CORRUPT="1")
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert [args[0] for args in backup.restic_calls()][-1] == "restore"
    assert backup.known_good.read_text() == "previous verified backup"


def test_active_writer_prevents_capture(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup(BIZ_TEST_ACTIVE="1")
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert all("pg_dump" not in args for args in backup.commands)
    assert all(args[0] != "backup" for args in backup.restic_calls())
    assert any("--all" in args for args in backup.commands if "--status" in args)


def test_missing_local_object_store_prevents_capture(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup(BIZ_STORAGE_ROOT="")
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert all("pg_dump" not in args for args in backup.commands)
    assert all(args[0] != "backup" for args in backup.restic_calls())


def test_storage_root_follows_compose_precedence(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup(BIZ_TEST_USE_HOST_ROOT="1")
    assert backup.result.returncode == 0, backup.result.stderr
    with tarfile.open(backup.snapshot / "objects.tar") as archive:
        assert "./saved.txt" in archive.getnames()


def test_installed_storage_mount_drift_prevents_capture(
    run_backup: Callable[..., BackupRun],
) -> None:
    backup = run_backup(BIZ_TEST_USE_HOST_ROOT="1", BIZ_TEST_MOUNT_DRIFT="1")
    assert backup.result.returncode != 0
    assert "object mount differs" in backup.result.stderr
    assert all("pg_dump" not in args for args in backup.commands)
    assert not backup.restic_calls()


def test_legacy_storage_root_with_drifted_mount_prevents_capture(
    run_backup: Callable[..., BackupRun], tmp_path: Path
) -> None:
    # A pre-PR-02 .env sets only BIZ_STORAGE_ROOT while Compose mounts ../var/objects.
    installed = tmp_path / "var-objects"
    installed.mkdir()
    backup = run_backup(BIZ_TEST_INSTALLED_ROOT=str(installed))
    assert backup.result.returncode != 0, "backed up a directory the containers do not use"
    assert "object mount" in backup.result.stderr
    assert f"BIZ_STORAGE_HOST_ROOT={installed.resolve()}" in backup.result.stderr
    assert all("pg_dump" not in args for args in backup.commands)
    assert not backup.restic_calls()


def test_legacy_storage_root_matching_the_mount_still_backs_up(
    run_backup: Callable[..., BackupRun], tmp_path: Path
) -> None:
    backup = run_backup(BIZ_TEST_INSTALLED_ROOT=str(tmp_path / "documents"))
    assert backup.result.returncode == 0, backup.result.stderr


def test_failed_staging_cleanup_prevents_success(run_backup: Callable[..., BackupRun]) -> None:
    backup = run_backup(BIZ_TEST_FAILURE="cleanup")
    assert backup.result.returncode != 0, backup.result.stdout
    assert "backup ok" not in backup.result.stdout
    assert backup.result.stderr
    assert backup.known_good.read_text() == "previous verified backup"
