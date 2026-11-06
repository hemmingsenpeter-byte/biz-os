"""Pack loader: reads a composition, validates every part, registers packet schemas."""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from sqlalchemy import MetaData, Table, insert, inspect
from sqlalchemy.engine import Engine

from biz_kernel.db import utcnow, uuid7
from biz_kernel.packets.bus import SchemaRegistry

KERNEL_VERSION = "1.0.0"
SCHEMA_DIR = Path(__file__).parent / "schemas"
_CADENCE = re.compile(r"^(\d+)([smhd])$")
_TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class LoadError(Exception):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


@dataclass
class CapabilityInfo:
    name: str
    version: str
    path: Path
    ports: list[str]
    tools: dict[str, str]  # tool name -> side effect
    publishes: list[str]
    consumes: list[str]
    screens: list[str]


@dataclass
class TopicInfo:
    name: str
    sop_path: Path
    tools: list[str]
    sop: dict[str, Any]
    decision_types: list[str]


@dataclass
class RoleInfo:
    name: str
    version: str
    path: Path
    mission: str
    goals: list[str]
    heartbeat_cadence: str
    heartbeat_triggers: list[str]
    budget_usd_month: float
    topics: list[TopicInfo]
    consumes: list[str]
    publishes: list[str]
    evidence: dict[str, list[str]]
    slots: list[str]
    sources: list[str]
    autonomy_defaults: dict[str, int]
    model_tier: dict[str, str]
    escalate_when: list[Any]
    requires: dict[str, Any]

    @property
    def allow_list(self) -> list[str]:
        """Union of the tools of every topic, in first-seen order."""
        seen: dict[str, None] = {}
        for t in self.topics:
            for tool in t.tools:
                seen.setdefault(tool)
        return list(seen)


@dataclass
class IndustryPack:
    name: str
    version: str
    path: Path
    ext_prefix: str
    extension_files: list[Path]
    fills: dict[str, Path]
    requires: dict[str, Any]

    @property
    def table_prefix(self) -> str:
        return f"ext_{self.ext_prefix}_"


@dataclass
class Manifest:
    name: str
    kernel: str
    kernel_version: str
    app_dir: Path
    capabilities: dict[str, CapabilityInfo]
    ports: dict[str, str]
    roles: dict[str, RoleInfo]
    industries: dict[str, IndustryPack]
    slot_fills: dict[str, tuple[str, Path]]
    packet_types: list[str]
    tools: dict[str, tuple[str, str]]  # tool name -> (capability, side effect)
    policy: dict[str, Any]
    goals: dict[str, Any]
    registry: SchemaRegistry = field(repr=False, default_factory=SchemaRegistry)

    def role_budget(self, role: str) -> float:
        budgets = self.policy.get("budgets_usd_month") or {}
        return float(budgets.get(role, self.roles[role].budget_usd_month))

    def render(self) -> str:
        lines = [f"{self.name}  (kernel {self.kernel_version}, wants {self.kernel})"]
        lines.append(f"capabilities: {', '.join(self.capabilities) or '-'}")
        lines.append("ports: " + (", ".join(f"{k}={v}" for k, v in self.ports.items()) or "-"))
        lines.append("roles:")
        for r in self.roles.values():
            topics = ", ".join(t.name for t in r.topics)
            lines.append(
                f"  {r.name} v{r.version}  heartbeat {r.heartbeat_cadence}  "
                f"budget ${self.role_budget(r.name):g}/mo"
            )
            lines.append(f"    topics: {topics}")
            lines.append(
                f"    consumes: {', '.join(r.consumes)}  publishes: {', '.join(r.publishes)}"
            )
            lines.append(f"    allow-list: {', '.join(r.allow_list)}")
        lines.append(
            "industries: " + ", ".join(f"{i.name} v{i.version}" for i in self.industries.values())
        )
        lines.append(
            "slot fills: " + ", ".join(f"{s}<-{i}" for s, (i, _) in self.slot_fills.items())
        )
        lines.append(f"packet types: {len(self.packet_types)}")
        lines.append(f"tools: {', '.join(f'{t} [{se}]' for t, (_, se) in self.tools.items())}")
        return "\n".join(lines)


def parse_cadence(text: str) -> timedelta:
    """'30m' -> 30 minutes. Units: s, m, h, d."""
    m = _CADENCE.match(text)
    if not m:
        raise ValueError(f"bad cadence {text!r}")
    return timedelta(
        **{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m[2]]: int(m[1])}
    )


def kernel_specifier(text: str) -> SpecifierSet:
    """Accepts '>=1.0,<2' or the short form '1.x'."""
    m = re.fullmatch(r"(\d+)\.x", text.strip())
    if m:
        return SpecifierSet(f">={m[1]},<{int(m[1]) + 1}")
    return SpecifierSet(text)


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def _read(path: Path, schema: str | None, problems: list[str]) -> dict[str, Any] | None:
    """Read YAML and validate against a named schema; record problems, never raise."""
    if not path.is_file():
        problems.append(f"missing file: {path}")
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        problems.append(f"{path}: invalid YAML: {exc}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{path}: expected a mapping")
        return None
    if schema:
        validator = jsonschema.Draft202012Validator(_schema(schema))
        errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        for e in errs:
            where = "/".join(str(p) for p in e.path) or "(root)"
            problems.append(f"{path}: {where}: {e.message}")
        if errs:
            return None
    return data


def _check_kernel(what: str, spec: str | None, problems: list[str]) -> None:
    if not spec:
        return
    try:
        ok = Version(KERNEL_VERSION) in kernel_specifier(spec)
    except InvalidSpecifier:
        problems.append(f"{what}: bad kernel version range {spec!r}")
        return
    if not ok:
        problems.append(f"{what}: needs kernel {spec} but kernel is {KERNEL_VERSION}")


def _templates(value: Any) -> list[str]:
    if isinstance(value, str):
        return _TEMPLATE.findall(value)
    if isinstance(value, dict):
        return [t for v in value.values() for t in _templates(v)]
    if isinstance(value, list):
        return [t for v in value for t in _templates(v)]
    return []


def _check_sop(
    sop: dict[str, Any],
    where: Path,
    topic_tools: list[str],
    role: dict[str, Any],
    tools: dict[str, tuple[str, str]],
    goals: set[str],
    problems: list[str],
) -> None:
    if sop["trigger"] not in role["consumes"]:
        problems.append(f"{where}: trigger {sop['trigger']} not in role consumes")
    if sop["goal_key"] not in goals:
        problems.append(f"{where}: goal_key {sop['goal_key']} not in goals.yaml")
    earlier: set[str] = set()
    for step in sop["steps"]:
        sid = step["id"]
        if sid in earlier:
            problems.append(f"{where}: duplicate step id {sid}")
        if step["kind"] == "tool":
            tool = step["tool"]
            if tool not in tools:
                problems.append(f"{where}: step {sid}: tool {tool} not in any loaded capability")
            if tool not in topic_tools:
                problems.append(f"{where}: step {sid}: tool {tool} not in the topic's tools")
        if step["kind"] == "publish" and step["packet"] not in role["publishes"]:
            problems.append(
                f"{where}: step {sid}: publishes {step['packet']} not in role publishes"
            )
        if step["kind"] == "draft":
            prompt = where.parent.parent / step["prompt"]
            if not prompt.is_file():
                problems.append(f"{where}: step {sid}: prompt file missing: {step['prompt']}")
        for t in _templates({k: v for k, v in step.items() if k not in ("schema", "id")}):
            parts = t.split(".")
            if parts[0] == "steps" and (len(parts) < 3 or parts[1] not in earlier):
                problems.append(
                    f"{where}: step {sid}: template {{{{{t}}}}} refers to an unknown earlier step"
                )
            elif parts[0] not in ("steps", "packet"):
                problems.append(
                    f"{where}: step {sid}: template {{{{{t}}}}} must start with packet or steps"
                )
        earlier.add(sid)


def load_composition(app_dir: Path, repo_root: Path) -> Manifest:
    """Load and validate an app composition; raise LoadError listing every problem."""
    app_dir, repo_root = Path(app_dir), Path(repo_root)
    problems: list[str] = []
    comp = _read(app_dir / "composition.yaml", "composition", problems)
    if comp is None:
        raise LoadError(problems)
    _check_kernel("composition", comp["kernel"], problems)
    policy = _read(app_dir / comp["policy"], None, problems) or {}
    goals_doc = _read(app_dir / comp["goals"], None, problems) or {}
    goal_keys = {g["key"] for g in goals_doc.get("goals", []) if isinstance(g, dict) and "key" in g}
    cap_names = set(comp["capabilities"])
    port_kinds = set(comp["ports"])

    registry = SchemaRegistry()
    try:
        registry.load_dir(repo_root / "packets")
    except (jsonschema.SchemaError, json.JSONDecodeError, ValueError) as exc:
        problems.append(f"packets/: {exc}")
    packet_types = sorted({t for t, _ in registry.known()})

    caps: dict[str, CapabilityInfo] = {}
    tools: dict[str, tuple[str, str]] = {}
    for c in comp["capabilities"]:
        d = _read(repo_root / "capabilities" / c / "capability.yaml", "capability", problems)
        if d is None:
            continue
        what = f"capability {c}"
        _check_kernel(what, d["requires"].get("kernel"), problems)
        for p in d["requires"].get("ports", []):
            if p not in port_kinds:
                problems.append(f"{what}: needs port {p} but composition has none")
        for p in d["requires"].get("capabilities", []):
            if p not in cap_names:
                problems.append(f"{what}: needs capability {p} which is not in the composition")
        for t in d["tools"]:
            tools[t["name"]] = (c, t["side_effect"])
        caps[c] = CapabilityInfo(
            d["name"],
            d["version"],
            repo_root / "capabilities" / c,
            d["requires"].get("ports", []),
            {t["name"]: t["side_effect"] for t in d["tools"]},
            d["publishes"],
            d["consumes"],
            d.get("screens", []),
        )

    industries: dict[str, IndustryPack] = {}
    slot_fills: dict[str, tuple[str, Path]] = {}
    for i in comp["industries"]:
        base = repo_root / "industries" / i
        d = _read(base / "industry.yaml", "industry", problems)
        if d is None:
            continue
        what = f"industry {i}"
        _check_kernel(what, d["requires"].get("kernel"), problems)
        for p in d["requires"].get("capabilities", []):
            if p not in cap_names:
                problems.append(f"{what}: needs capability {p} which is not in the composition")
        fills: dict[str, Path] = {}
        for slot, rel in d["fills"].items():
            fills[slot] = base / rel
            if not (base / rel).exists():
                problems.append(f"{what}: slot {slot} file missing: {rel}")
            if slot in slot_fills:
                problems.append(f"slot {slot} filled by both {slot_fills[slot][0]} and {i}")
            slot_fills[slot] = (i, base / rel)
        ext_files = [base / e for e in d.get("extensions", [])]
        for e in ext_files:
            if not e.is_file():
                problems.append(f"{what}: extension file missing: {e.relative_to(base)}")
        industries[i] = IndustryPack(
            d["industry"],
            d["version"],
            base,
            d.get("ext_prefix", d["industry"]),
            ext_files,
            fills,
            d["requires"],
        )

    roles: dict[str, RoleInfo] = {}
    for r in comp["roles"]:
        base = repo_root / "roles" / r
        d = _read(base / "role.yaml", "role", problems)
        if d is None:
            continue
        what = f"role {r}"
        _check_kernel(what, d["requires"].get("kernel"), problems)
        for p in d["requires"].get("capabilities", []):
            if p not in cap_names:
                problems.append(f"{what}: needs capability {p} which is not in the composition")
        for s in d["slots"]:
            if s not in slot_fills:
                problems.append(f"{what}: slot {s} is not filled by any loaded industry pack")
        for t in {*d["consumes"], *d["publishes"]} - {"*"}:
            if t not in packet_types:
                problems.append(f"{what}: packet type {t} has no schema in packets/")
        for g in d["goals"]:
            if g not in goal_keys:
                problems.append(f"{what}: goal {g} not in goals.yaml")
        topics: list[TopicInfo] = []
        for t in d["topics"]:
            for tool in t["tools"]:
                if tool not in tools:
                    problems.append(
                        f"{what}: topic {t['name']}: tool {tool} not in any loaded capability"
                    )
            sop_path = base / t["sop"]
            sop = _read(sop_path, "sop", problems)
            dts: list[str] = []
            if sop is not None:
                _check_sop(sop, sop_path, t["tools"], d, tools, goal_keys, problems)
                dts = [s["decision_type"] for s in sop["steps"] if "decision_type" in s]
            topics.append(TopicInfo(t["name"], sop_path, t["tools"], sop or {}, dts))
        if not (base / "tests" / "golden").is_dir():
            problems.append(f"{what}: tests/golden/ missing")
        hb = d["heartbeat"]
        roles[r] = RoleInfo(
            d["role"],
            d["version"],
            base,
            d["mission"],
            d["goals"],
            hb["cadence"],
            hb["triggers"],
            d["budget_usd_month"],
            topics,
            d["consumes"],
            d["publishes"],
            d["evidence"],
            d["slots"],
            d.get("sources", []),
            d.get("autonomy_defaults", {}),
            d.get("model_tier", {}),
            d.get("escalate_when", []),
            d["requires"],
        )

    for iname, pack in industries.items():
        try:
            check_extensions(pack, load_extension_tables(pack).keys())
        except Exception as exc:  # report and keep going
            problems.append(f"industry {iname}: {exc}")

    if problems:
        raise LoadError(problems)
    return Manifest(
        comp["name"],
        comp["kernel"],
        KERNEL_VERSION,
        app_dir,
        caps,
        comp["ports"],
        roles,
        industries,
        slot_fills,
        packet_types,
        tools,
        policy,
        goals_doc,
        registry,
    )


# ---- pack migrations -------------------------------------------------------
# Extension files are Python modules exposing `define(metadata: MetaData) -> None` that adds
# Tables to the pack's own MetaData. Alembic branches per pack come later; for now install
# creates the tables directly and uninstall drops only them.


def load_extension_tables(pack: IndustryPack) -> dict[str, Table]:
    """Run each extension file's define() against one fresh MetaData for the pack."""
    md = MetaData()
    for path in pack.extension_files:
        spec = importlib.util.spec_from_file_location(f"_ext_{pack.name}_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.define(md)
    return dict(md.tables)


def check_extensions(pack: IndustryPack, tables: Any) -> None:
    """Every table a pack declares must start with ext_<prefix>_."""
    bad = [t for t in tables if not t.startswith(pack.table_prefix)]
    if bad:
        raise LoadError(
            [
                f"pack {pack.name} declares table {t}; names must start with {pack.table_prefix}"
                for t in bad
            ]
        )


def _own_tables(pack: IndustryPack, engine: Engine | None = None) -> tuple[MetaData, list[Table]]:
    tables = load_extension_tables(pack)
    check_extensions(pack, tables)
    md = next(iter(tables.values())).metadata if tables else MetaData()
    own = list(tables.values())
    if engine is not None:  # reflect referenced foreign tables so keys resolve; never created
        for t in own:
            for fk in t.foreign_keys:
                name = fk.target_fullname.split(".")[0]
                if name not in md.tables:
                    Table(name, md, autoload_with=engine)
    return md, own


def _audit_branch(engine: Engine, pack: IndustryPack, kind: str) -> None:
    """Record the pack's Alembic branch label (`pack_<name>`) in the append-only audit log."""
    from biz_kernel.audit.log import AuditEntry
    from biz_kernel.migrations import pack_branch

    if not inspect(engine).has_table(AuditEntry.__tablename__):
        return
    with engine.begin() as conn:
        conn.execute(
            insert(AuditEntry.__table__),
            [
                {
                    "id": uuid7(),
                    "at": utcnow(),
                    "actor": "kernel.loader",
                    "kind": kind,
                    "subject": pack_branch(pack.name),
                    "detail": {"pack": pack.name, "version": pack.version},
                }
            ],
        )


def install_pack(engine: Engine, pack: IndustryPack) -> list[str]:
    """Create the pack's ext_ tables (after the ext_ guard), record its branch, load fixtures.

    A pack that ships real Alembic revisions in `<pack>/migrations/versions` (labelled
    `pack_<name>`) is upgraded through Alembic; define()-style packs are created directly.
    """
    md, own = _own_tables(pack, engine)
    versions = pack.path / "migrations" / "versions"
    if versions.is_dir():
        from biz_kernel import migrations
        from biz_kernel.migrations import pack_branch

        migrations.upgrade(
            engine.url.render_as_string(hide_password=False),
            f"{pack_branch(pack.name)}@head",
            [versions],
        )
    else:
        md.create_all(engine, tables=own)
    _audit_branch(engine, pack, "pack.install")
    by_name = {t.name: t for t in own}
    fixtures = sorted((pack.path / "fixtures").glob("*.yaml"))
    with engine.begin() as conn:
        for f in fixtures:
            for table, rows in (yaml.safe_load(f.read_text()) or {}).items():
                if table not in by_name:
                    raise LoadError(
                        [f"{f}: fixture targets {table}, not a table of pack {pack.name}"]
                    )
                if rows:
                    conn.execute(insert(by_name[table]), rows)
    return sorted(by_name)


def uninstall_pack(engine: Engine, pack: IndustryPack) -> list[str]:
    """Drop only the pack's own ext_ tables."""
    md, own = _own_tables(pack, engine)
    md.drop_all(engine, tables=own)
    _audit_branch(engine, pack, "pack.uninstall")
    return sorted(t.name for t in own)
