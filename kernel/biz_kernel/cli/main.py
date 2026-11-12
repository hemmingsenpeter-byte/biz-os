"""`biz` command line: load and doctor."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Annotated

import typer

from biz_kernel.loader import LoadError, Manifest, load_composition, parse_cadence

RepoRoot = Annotated[Path | None, typer.Option("--repo-root")]

app = typer.Typer(help="BIZ OS command line", no_args_is_help=True)


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to the directory holding packets/ and roles/."""
    for p in [start.resolve(), *start.resolve().parents]:
        if (p / "packets").is_dir() and (p / "roles").is_dir():
            return p
    raise typer.BadParameter(f"no repo root above {start}")


def _load(app_dir: Path, repo_root: Path | None) -> tuple[Manifest, Path]:
    root = repo_root or find_repo_root(app_dir)
    try:
        return load_composition(app_dir, root), root
    except LoadError as exc:
        for p in exc.problems:
            typer.echo(f"✗ {p}")
        raise typer.Exit(1) from exc


@app.command("load")
def load_cmd(
    app_dir: Path,
    repo_root: RepoRoot = None,
) -> None:
    """Load a composition and print its manifest."""
    manifest, _ = _load(app_dir, repo_root)
    typer.echo(manifest.render())


def _check_port(impl: str) -> tuple[bool, str]:
    """Fakes must be keys of biz_port_fakes.FAKES; real ports importable with a create()."""
    if impl.startswith("fake_"):
        try:
            from biz_port_fakes import FAKES
        except ImportError:
            return False, "biz_port_fakes is not importable"
        return (impl in FAKES), ("" if impl in FAKES else f"{impl} not in biz_port_fakes.FAKES")
    try:
        mod = importlib.import_module(f"biz_port_{impl}")
    except ImportError:
        return False, f"package biz_port_{impl} is not importable"
    if callable(getattr(mod, "create", None)):
        return True, ""
    # Accept a class exposing create() (the adapter classes do this).
    for obj in vars(mod).values():
        if isinstance(obj, type) and callable(getattr(obj, "create", None)):
            return True, ""
    return False, f"biz_port_{impl} has no create()"


def _check_capability(name: str) -> tuple[bool, str]:
    try:
        mod = importlib.import_module(f"biz_cap_{name}")
    except ImportError:
        return False, f"package biz_cap_{name} is not importable"
    return (callable(getattr(mod, "register", None)), f"biz_cap_{name} has no register()")


@app.command("doctor")
def doctor_cmd(
    app_dir: Path,
    repo_root: RepoRoot = None,
) -> None:
    """Validate packs, ports, capabilities, budgets and heartbeats. Exit 1 on any failure."""
    root = repo_root or find_repo_root(app_dir)
    try:
        manifest = load_composition(app_dir, root)
    except LoadError as exc:
        for p in exc.problems:
            typer.echo(f"✗ {p}")
        raise typer.Exit(1) from exc
    typer.echo(
        f"✓ composition {manifest.name} loads: {len(manifest.roles)} roles, "
        f"{len(manifest.capabilities)} capabilities, {len(manifest.packet_types)} packet types"
    )
    failed = False

    def report(ok: bool, msg: str) -> None:
        nonlocal failed
        failed = failed or not ok
        typer.echo(f"{'✓' if ok else '✗'} {msg}")

    for kind, impl in manifest.ports.items():
        ok, why = _check_port(impl)
        report(ok, f"port {kind} -> {impl}" + ("" if ok else f": {why}"))
    for cap in manifest.capabilities:
        ok, why = _check_capability(cap)
        report(ok, f"capability {cap}" + ("" if ok else f": {why}"))
    for r in manifest.roles:
        report(manifest.role_budget(r) > 0, f"budget set for role {r}")
        cadence = manifest.roles[r].heartbeat_cadence
        try:
            parse_cadence(cadence)
            report(True, f"heartbeat cadence {cadence} for role {r}")
        except ValueError:
            report(False, f"heartbeat cadence {cadence!r} for role {r} does not parse")
    from biz_kernel.policy.engine import limit_problems

    problems = limit_problems(manifest.policy.get("limits") or {})
    report(not problems, "money limits are usable" + "".join(f"\n  {p}" for p in problems))
    if failed:
        raise typer.Exit(1)


@app.command("migrate")
def migrate_cmd(
    app_dir: Path,
    repo_root: RepoRoot = None,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
) -> None:
    """Upgrade the database to head (kernel branch), after checking the composition loads."""
    from biz_kernel import db, migrations

    _load(app_dir, repo_root)
    target = url or db.database_url()
    migrations.upgrade(target)
    typer.echo("✓ database is at head")


def _switch(app_dir: Path, repo_root: Path | None, url: str | None, on: bool, reason: str) -> None:
    from biz_kernel import control, db

    _load(app_dir, repo_root)
    db.configure(url or db.database_url())
    with db.session_scope() as s:
        state = (control.pause if on else control.resume)(s, "cli", reason)
    typer.echo("paused: no role runs and nothing is sent" if state.paused else "running")


@app.command("pause")
def pause_cmd(
    app_dir: Path,
    reason: Annotated[str, typer.Option(help="Why, for the audit log")] = "",
    repo_root: RepoRoot = None,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
) -> None:
    """The kill switch: pause every role. Inbound work is kept and runs after `biz resume`."""
    _switch(app_dir, repo_root, url, True, reason)


@app.command("resume")
def resume_cmd(
    app_dir: Path,
    repo_root: RepoRoot = None,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
) -> None:
    """Turn the kill switch off."""
    _switch(app_dir, repo_root, url, False, "")


@app.command("worker")
def worker_cmd(app_dir: Path, repo_root: RepoRoot = None) -> None:
    """Run the worker (migrations, DBOS, heartbeat loop) for an app directory."""
    root = repo_root or find_repo_root(app_dir)
    os.environ["BIZ_APP_DIR"] = str(app_dir)
    os.environ["BIZ_REPO_ROOT"] = str(root)
    from biz_kernel.workflows import worker

    worker.main()


# ---- biz eval: provided by biz_kernel.models.eval.cli (a Typer sub-app) -----------------
try:
    from biz_kernel.models.eval.cli import eval_cmd as _eval_cmd

    @app.command("eval")
    def eval_task(
        task: str,
        tier: Annotated[str, typer.Option(help="local | cloud | shadow")] = "local",
        cases: Annotated[Path, typer.Option(help="Folder of golden case YAML files")] = Path("."),
    ) -> None:
        """Score a model task on golden cases."""
        _eval_cmd(task, tier, cases)
except ImportError:  # eval harness not installed yet
    pass


if __name__ == "__main__":
    app()
