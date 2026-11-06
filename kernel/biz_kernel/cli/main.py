"""`biz` command line: load and doctor."""

from __future__ import annotations

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


def _port_available(name: str, root: Path) -> bool:
    if (root / "ports" / name).is_dir():
        return True
    fakes = root / "ports" / "fakes"
    return any(name in p.read_text() or p.stem == name for p in fakes.rglob("*.py")) or any(
        d.name == name for d in fakes.rglob("*") if d.is_dir()
    )


@app.command("doctor")
def doctor_cmd(
    app_dir: Path,
    repo_root: RepoRoot = None,
) -> None:
    """Validate packs, ports, budgets and heartbeat cadences. Exit 1 on any failure."""
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
        report(_port_available(impl, root), f"port {kind} -> {impl}")
    budgets = manifest.policy.get("budgets_usd_month") or {}
    for r in manifest.roles:
        report(budgets.get(r, 0) > 0, f"budget set for role {r} in policy.yaml")
        cadence = manifest.roles[r].heartbeat_cadence
        try:
            parse_cadence(cadence)
            report(True, f"heartbeat cadence {cadence} for role {r}")
        except ValueError:
            report(False, f"heartbeat cadence {cadence!r} for role {r} does not parse")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
