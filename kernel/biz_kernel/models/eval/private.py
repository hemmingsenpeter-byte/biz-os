"""Owner-controlled, offline preparation of private historical triage cohorts."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import yaml

TASK = "receptionist.triage"
CATEGORIES = {"enquiry", "service_request", "supplier", "spam", "other"}
MIN_CASES = 100
MIN_HELDOUT = 50
# Mirror the intake routing boundary for categories that would otherwise become "other".
TRIAGE_ROUTING_MIN_CONFIDENCE = 0.6
SOURCE_FIELDS = ("thread_key", "channel", "subject", "body")
REVIEW_FIELDS = ("case_id", "channel", "subject", "body", "category")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.UNICODE)
URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
PHONE = re.compile(r"(?<!\w)\+?\d[\d ()\-.]{5,}\d(?!\w)")


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


def confirm(workspace: Path, version: str, owner: str, repo_root: Path) -> dict[str, int]:
    """Bind explicit owner-confirmed labels to immutable case payloads."""
    from biz_kernel.models.eval.harness import dataset_metadata, load_cases

    root = private_path(workspace)
    version_dir = _version_dir(root, version)
    if not OPAQUE_ID.fullmatch(owner):
        raise ValueError("owner must be a non-personal identifier")
    prompt = (repo_root / "roles/receptionist/prompts/triage.md").read_text()
    schema = (repo_root / "roles/receptionist/tests/eval/triage_heldout/_schema.yaml").read_text()
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
            if any(row["category"] not in CATEGORIES for row in rows):
                raise ValueError("owner must confirm a valid category for every case")
            datasets[split] = rows

        with tempfile.TemporaryDirectory(dir=version_dir, prefix=".confirm-") as temporary:
            confirmed = Path(temporary) / "confirmed"
            for split, rows in datasets.items():
                split_dir = confirmed / split
                write_private(split_dir / "_prompt.md", prompt)
                write_private(split_dir / "_schema.yaml", schema)
                for row in rows:
                    category = row["category"]
                    checks: list[dict[str, Any]] = [
                        {"type": "json_schema"},
                        {"type": "exact", "fields": ["category"]},
                    ]
                    if category != "other":
                        checks.append(
                            {
                                "type": "numeric_min",
                                "field": "confidence",
                                "min": TRIAGE_ROUTING_MIN_CONFIDENCE,
                            }
                        )
                    case = {
                        "name": row["case_id"],
                        "system_file": "_prompt.md",
                        "schema_file": "_schema.yaml",
                        "untrusted": [
                            json.dumps(
                                {"enquiry": {"from": "[redacted]", **cases[row["case_id"]]}},
                                ensure_ascii=False,
                            )
                        ],
                        "expected": {"category": category},
                        "checks": checks,
                    }
                    write_private(split_dir / f"{row['case_id']}.yaml", yaml.safe_dump(case))
                task = {
                    "schema_version": 1,
                    "dataset": "historical-receptionist-triage",
                    "dataset_version": version,
                    "task": TASK,
                    "decision_type": "enquiry_classification",
                    "split": split,
                    "provenance": "historical",
                    "private": True,
                }
                hashes = dataset_metadata([], load_cases(split_dir))
                receipt = {
                    **task,
                    "owner": owner,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "case_count": len(rows),
                    "source_sha256": provenance["case_sha256"],
                    "dataset_sha256": hashes["dataset_sha256"],
                }
                write_private(split_dir / "_task.yaml", yaml.safe_dump(task))
                write_private(
                    split_dir / "_owner_review.json", json.dumps(receipt, indent=2) + "\n"
                )
            confirmed.rename(version_dir / "confirmed")
    return {split: len(rows) for split, rows in datasets.items()}
