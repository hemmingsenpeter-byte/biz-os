"""`biz` command line: load and doctor."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

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


@app.command("owner-invite")
def owner_invite_cmd(
    app_dir: Path,
    email: Annotated[str, typer.Option("--email", help="Email of the first owner.")],
    name: Annotated[str, typer.Option("--name", help="Display name for a new owner.")] = "Owner",
    reissue: Annotated[
        bool, typer.Option("--reissue", help="Issue a fresh invite for an existing active owner.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the invitation as JSON.")
    ] = False,
    repo_root: RepoRoot = None,
    url: Annotated[str | None, typer.Option("--url", envvar="BIZ_DATABASE_URL")] = None,
) -> None:
    """Create the first owner and print a single-use passkey enrolment link.

    Run locally against an already migrated database. This command sends no mail.
    Use --reissue to recover an interrupted enrolment for the same active owner.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from biz_kernel import db
    from biz_kernel.audit.log import record
    from biz_kernel.auth import config, tokens
    from biz_kernel.auth.users import prepare_owner_invite

    _load(app_dir, repo_root)
    try:
        config.secret_key()  # fail before creating a user if signing is not configured
    except RuntimeError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    engine = create_engine(url or db.database_url())
    try:
        with Session(engine) as session, session.begin():
            user = prepare_owner_invite(session, email, name, reissue=reissue)
            token = tokens.issue_invite(user.id)
            invite_url = (
                f"{config.origin().rstrip('/')}/auth/register?{urlencode({'invite': token})}"
            )
            invite = {
                "user_id": str(user.id),
                "email": user.email,
                "invite_url": invite_url,
                "expires_in_seconds": config.INVITE_MAX_AGE,
            }
            record(
                session,
                "cli",
                "auth",
                "owner_invite_reissued" if reissue else "first_owner_invited",
                user_id=user.id,
                email=user.email,
            )
    except ValueError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()
    # The database transaction committed before the bearer invitation is exposed.
    if json_output:
        typer.echo(json.dumps(invite))
    else:
        typer.echo(f"Owner invitation for {invite['email']} (single use, valid for 24 hours):")
        typer.echo(invite["invite_url"])


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
