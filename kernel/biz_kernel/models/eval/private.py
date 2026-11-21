"""Owner-controlled, offline preparation of private historical classification cohorts.

The kernel names no role. `confirm` reads the task, label field, categories, prompt and
schema from a role's packaged evaluation directory: its `_schema.yaml` and the
`historical` block of its `_task.yaml`.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import yaml

DEFAULT_THRESHOLD = 0.05
CLOUD_AUDIT_FILE = "cloud_disclosures.jsonl"
MIN_CASES = 100
MIN_HELDOUT = 50
SOURCE_FIELDS = ("thread_key", "channel", "subject", "body")
REVIEW_FIELDS = ("case_id", "channel", "subject", "body", "category")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.UNICODE)
URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
PHONE = re.compile(r"(?<!\w)\+?\d[\d ()\-.]{5,}\d(?!\w)")


class Template(TypedDict):
    task: dict[str, Any]
    dataset: str
    prompt: str
    prompt_source: str | None
    schema: str
    label_field: str
    categories: set[str]
    payload_key: str
    checks: list[dict[str, Any]]


class SplitLedger(TypedDict):
    salt: str
    threads: dict[str, str]
    texts: dict[str, str]
    splits: dict[str, str]


def private_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if any((directory / ".git").exists() for directory in (path, *path.parents)):
        raise ValueError("private evaluation files must be outside Git working trees")
    return path


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".eval-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def find_repo_root(start: Path) -> Path | None:
    """The nearest directory above `start` holding packets/ and roles/, if any."""
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / "packets").is_dir() and (directory / "roles").is_dir():
            return directory
    return None


def _repo_relative(path: Path) -> str | None:
    root = find_repo_root(path.parent)
    if root is None or not path.resolve().is_relative_to(root):
        return None
    return path.resolve().relative_to(root).as_posix()


def sha_json(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def csv_rows(path: Path, fields: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != fields:
                raise ValueError(f"CSV columns must be {', '.join(fields)}")
            rows = list(reader)
    except csv.Error as exc:
        raise ValueError("invalid evaluation CSV") from exc
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("CSV contains an incomplete or extra column")
    return rows


def csv_text(fields: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _redact(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip()
    text = URL.sub("[link]", text)
    text = EMAIL.sub("[email]", text)
    return PHONE.sub("[phone]", text)


def spreadsheet_text(text: str) -> str:
    # Prevent formula execution if an owner opens the review CSV in a spreadsheet.
    return "[text] " + text if text.startswith(("=", "+", "-", "@")) else text


@contextmanager
def _workspace(root: Path) -> Iterator[None]:
    from fcntl import LOCK_EX, LOCK_UN, flock

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("the private workspace directory must have mode 0700")
    with (root / ".eval-lock").open("a") as lock:
        flock(lock, LOCK_EX)
        try:
            yield
        finally:
            flock(lock, LOCK_UN)


def _version_dir(root: Path, version: str) -> Path:
    if not VERSION.fullmatch(version):
        raise ValueError("version must be an opaque identifier of up to 64 characters")
    return private_path(root / version)


def _identity(salt: str, row: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    if not OPAQUE_ID.fullmatch(row["thread_key"]):
        raise ValueError("use an opaque, non-personal thread key for each enquiry")
    if row["channel"] not in {"mail", "form", "sms"}:
        raise ValueError("channel must be mail, form or sms")
    subject, body = _redact(row["subject"]), _redact(row["body"])
    if not body:
        raise ValueError("historical cases require a message body")
    subject_for_match = re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", subject, flags=re.IGNORECASE)
    normalized = [" ".join(text.casefold().split()) for text in (subject_for_match, body)]
    thread_key = hmac.new(bytes.fromhex(salt), row["thread_key"].encode(), "sha256").hexdigest()
    return (
        thread_key,
        sha_json(normalized),
        {"channel": row["channel"], "subject": subject, "body": body},
    )


def prepare(source: Path, workspace: Path, version: str) -> dict[str, int]:
    """Prepare two unlabelled owner review sheets and reserve stable case assignments."""
    root = private_path(workspace)
    version_dir = _version_dir(root, version)
    raw = csv_rows(private_path(source), SOURCE_FIELDS)
    with _workspace(root):
        if version_dir.exists():
            raise ValueError("dataset version already exists")
        ledger_file = root / "splits.json"
        ledger: SplitLedger = (
            json.loads(ledger_file.read_text())
            if ledger_file.exists()
            else {"salt": secrets.token_hex(32), "threads": {}, "texts": {}, "splits": {}}
        )
        threads: dict[str, str] = ledger["threads"]
        texts: dict[str, str] = ledger["texts"]
        splits: dict[str, str] = ledger["splits"]
        cases: dict[str, dict[str, str]] = {}
        rows = [_identity(ledger["salt"], row) for row in raw]
        for thread, fingerprint, content in sorted(
            rows, key=lambda row: (row[0], row[1], *row[2].values())
        ):
            matches = {
                case_id for case_id in (threads.get(thread), texts.get(fingerprint)) if case_id
            }
            if len(matches) > 1:
                raise ValueError("a duplicate connects previously distinct cases; exclude it")
            case_id = next(iter(matches), thread)
            threads[thread] = texts[fingerprint] = case_id
            cases.setdefault(case_id, content)
        if len(cases) < MIN_CASES:
            raise ValueError("at least 100 distinct historical enquiries are required")

        heldout_goal = max(MIN_HELDOUT, (len(cases) + 1) // 2)
        heldout_count = sum(splits.get(case_id) == "heldout" for case_id in cases)
        for case_id in sorted(cases.keys() - splits.keys()):
            split = "heldout" if heldout_count < heldout_goal else "tuning"
            splits[case_id] = split
            heldout_count += split == "heldout"
        if heldout_count < MIN_HELDOUT or heldout_count == len(cases):
            raise ValueError("retain at least 50 held-out enquiries and some tuning cases")

        with tempfile.TemporaryDirectory(dir=root, prefix=".prepare-") as temporary:
            prepared = Path(temporary) / version
            write_private(prepared / "prepared.json", json.dumps(cases))
            write_private(
                prepared / "provenance.json",
                json.dumps(
                    {"version": version, "count": len(cases), "case_sha256": sha_json(cases)}
                ),
            )
            for split in ("tuning", "heldout"):
                sheet = [
                    {
                        "case_id": case_id,
                        **{k: spreadsheet_text(v) for k, v in content.items()},
                        "category": "",
                    }
                    for case_id, content in sorted(cases.items())
                    if splits[case_id] == split
                ]
                write_private(prepared / split / "review.csv", csv_text(REVIEW_FIELDS, sheet))
            # Reserve identities before publishing sheets. A retry after a filesystem
            # interruption reuses those assignments.
            write_private(ledger_file, json.dumps(ledger, sort_keys=True))
            prepared.rename(version_dir)
    return {"imported": len(cases), "heldout": heldout_count, "duplicates": len(raw) - len(cases)}


def read_template(template: Path) -> Template:
    """Read a role's packaged evaluation directory as the template for private cases."""
    try:
        task = yaml.safe_load((template / "_task.yaml").read_text())
        schema = (template / "_schema.yaml").read_text()
        parsed = yaml.safe_load(schema)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("template needs readable _task.yaml and _schema.yaml files") from exc
    spec = task.get("historical") if isinstance(task, dict) else None
    if not isinstance(spec, dict) or not isinstance(parsed, dict):
        raise ValueError("template _task.yaml needs a historical block")
    if not all(isinstance(task.get(key), str) for key in ("task", "decision_type")):
        raise ValueError("template _task.yaml must name its task and decision_type")
    keys = ("dataset", "prompt_file", "label_field", "payload_key")
    if not all(isinstance(spec.get(key), str) and spec[key] for key in keys):
        raise ValueError(f"template historical block needs {', '.join(keys)}")
    label = spec["label_field"]
    properties, required = parsed.get("properties"), parsed.get("required")
    prop = properties.get(label) if isinstance(properties, dict) else None
    categories = prop.get("enum") if isinstance(prop, dict) else None
    if (
        not isinstance(categories, list)
        or not categories
        or not isinstance(required, list)
        or label not in required
    ):
        raise ValueError(f"template schema must require {label} as an enum")
    checks = spec.get("checks") or []
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        raise ValueError("template historical checks must be a list of mappings")
    threshold = task.get("threshold", DEFAULT_THRESHOLD)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("template shadow threshold must be finite and in [0, 1]")
    prompt_file = template / spec["prompt_file"]
    try:
        prompt = prompt_file.read_text()
    except OSError as exc:
        raise ValueError("template prompt_file is not readable") from exc
    return {
        "task": {
            **{k: task[k] for k in ("schema_version", "task", "decision_type") if k in task},
            "threshold": float(threshold),
        },
        "dataset": spec["dataset"],
        "prompt": prompt,
        "prompt_source": _repo_relative(prompt_file),
        "schema": schema,
        "label_field": label,
        "categories": {str(c) for c in categories},
        "payload_key": spec["payload_key"],
        "checks": [
            {"type": "json_schema"},
            {"type": "exact", "fields": [label]},
            *checks,
        ],
    }


def confirm(workspace: Path, version: str, owner: str, template: Path) -> dict[str, int]:
    """Bind explicit owner-confirmed labels to immutable case payloads.

    `template` is the role's packaged evaluation directory for the task.
    """
    from biz_kernel.models.eval.harness import dataset_sha256, load_cases

    root = private_path(workspace)
    version_dir = _version_dir(root, version)
    if not OPAQUE_ID.fullmatch(owner):
        raise ValueError("owner must be a non-personal identifier")
    spec = read_template(template)
    prompt, schema, label = spec["prompt"], spec["schema"], spec["label_field"]
    with _workspace(root):
        ledger = json.loads((root / "splits.json").read_text())["splits"]
        cases = json.loads((version_dir / "prepared.json").read_text())
        provenance = json.loads((version_dir / "provenance.json").read_text())
        if provenance != {"version": version, "count": len(cases), "case_sha256": sha_json(cases)}:
            raise ValueError("prepared cases no longer match their provenance")
        if (version_dir / "confirmed").exists():
            raise ValueError("this version already has confirmed owner labels")
        datasets: dict[str, list[dict[str, str]]] = {}
        for split in ("tuning", "heldout"):
            split_dir = version_dir / split
            rows = csv_rows(split_dir / "review.csv", REVIEW_FIELDS)
            expected = {
                case_id: {k: spreadsheet_text(v) for k, v in case.items()}
                for case_id, case in cases.items()
                if ledger[case_id] == split
            }
            reviewed = {
                row["case_id"]: {key: row[key] for key in SOURCE_FIELDS[1:]} for row in rows
            }
            if expected != reviewed or len(reviewed) != len(rows):
                raise ValueError("review sheet changed outside the category column")
            if any(row["category"] not in spec["categories"] for row in rows):
                raise ValueError(f"owner must confirm a valid {label} for every case")
            datasets[split] = rows

        with tempfile.TemporaryDirectory(dir=version_dir, prefix=".confirm-") as temporary:
            confirmed = Path(temporary) / "confirmed"
            for split, rows in datasets.items():
                split_dir = confirmed / split
                write_private(split_dir / "_prompt.md", prompt)
                write_private(split_dir / "_schema.yaml", schema)
                for row in rows:
                    case = {
                        "name": row["case_id"],
                        "system_file": "_prompt.md",
                        "schema_file": "_schema.yaml",
                        "untrusted": [
                            json.dumps(
                                {
                                    spec["payload_key"]: {
                                        "from": "[redacted]",
                                        **cases[row["case_id"]],
                                    }
                                },
                                ensure_ascii=False,
                            )
                        ],
                        "expected": {label: row["category"]},
                        "checks": spec["checks"],
                    }
                    write_private(split_dir / f"{row['case_id']}.yaml", yaml.safe_dump(case))
                task = {
                    "schema_version": 1,
                    **spec["task"],
                    "dataset": spec["dataset"],
                    "dataset_version": version,
                    "split": split,
                    "provenance": "historical",
                    "private": True,
                }
                digest = dataset_sha256(load_cases(split_dir))
                receipt = {
                    **task,
                    "owner": owner,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "case_count": len(rows),
                    "source_sha256": provenance["case_sha256"],
                    "dataset_sha256": digest,
                    # The prompt is frozen with the cases; reports compare it to the
                    # deployed prompt at `prompt_source` (relative to the repo root).
                    "prompt_sha256": sha_text(prompt),
                    "prompt_source": spec["prompt_source"],
                }
                write_private(split_dir / "_task.yaml", yaml.safe_dump(task))
                write_private(
                    split_dir / "_owner_review.json", json.dumps(receipt, indent=2) + "\n"
                )
            confirmed.rename(version_dir / "confirmed")
    return {split: len(rows) for split, rows in datasets.items()}


# ---- sending private cases to a cloud model: fails closed ----------------------------------


_ISSUED = object()  # only authorise_cloud_data holds this; see CloudAuthorisation


@dataclass(frozen=True)
class CloudAuthorisation:
    """The run flag, the target app's policy and a named operator all allow cloud data.

    Obtain one only from authorise_cloud_data(); direct construction is refused.
    """

    app: str
    operator: str
    token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.token is not _ISSUED:
            raise TypeError("a cloud data authorisation comes only from authorise_cloud_data()")


def cloud_data_problems(section: Any) -> list[str]:
    """Problems in the `eval:` section of an app's policy.yaml (biz doctor)."""
    if section is None:
        return []
    if not isinstance(section, Mapping):
        return ["eval must be a mapping"]
    problems = [f"eval.{key} is not a known setting" for key in section if key != "cloud_data"]
    if "cloud_data" in section and section["cloud_data"] not in ("allow", "deny"):
        problems.append("eval.cloud_data must be allow or deny")
    return problems


def cloud_data_allowed(policy: Mapping[str, Any]) -> bool:
    """Only an explicit, well-formed `eval: {cloud_data: allow}` allows it; absent denies."""
    section = policy.get("eval")
    return (
        isinstance(section, Mapping)
        and not cloud_data_problems(section)
        and section.get("cloud_data") == "allow"
    )


def authorise_cloud_data(
    *, allow_flag: bool, app_dir: Path | None, repo_root: Path | None, operator: str
) -> CloudAuthorisation:
    """Check every condition for sending private cases to a cloud model, or raise."""
    if not allow_flag:
        raise ValueError("private cloud evaluation requires --allow-cloud-data")
    if app_dir is None:
        raise ValueError(
            "private cloud evaluation also requires --app naming an app's policy that "
            "sets eval: {cloud_data: allow}"
        )
    from biz_kernel.loader.loader import LoadError, load_composition

    root = repo_root or find_repo_root(app_dir)
    if root is None:
        raise ValueError(f"no repo root above {app_dir}")
    try:
        manifest = load_composition(app_dir, root)
    except LoadError as exc:
        raise ValueError(f"app does not load: {'; '.join(exc.problems)}") from exc
    if not cloud_data_allowed(manifest.policy):
        raise ValueError(
            f"the policy of app {manifest.name} does not allow private evaluation data "
            "to be sent to a cloud model (eval.cloud_data)"
        )
    if not OPAQUE_ID.fullmatch(operator):
        raise ValueError("private cloud evaluation requires --operator, a non-personal identifier")
    return CloudAuthorisation(manifest.name, operator, _ISSUED)


def record_cloud_disclosure(workspace: Path, record: dict[str, Any]) -> Path:
    """Append one audit record (metadata only, never case content) to the workspace log."""
    root = private_path(workspace)
    path = root / CLOUD_AUDIT_FILE
    with _workspace(root):
        existing = path.read_text() if path.exists() else ""
        write_private(path, existing + json.dumps(record, sort_keys=True) + "\n")
    return path
