"""Evaluation schemas use the same declared JSON Schema dialect as runtime drafts."""

import asyncio

import jsonschema
import pytest
import yaml
from biz_kernel.models.eval.harness import load_cases, run_eval, score_case
from biz_kernel.models.router import Router
from biz_port_fakes import FakeModel

DRAFT4 = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "required": ["number"],
    "properties": {"number": {"$ref": "#/definitions/positive"}},
    "definitions": {"positive": {"type": "number", "minimum": 0, "exclusiveMinimum": True}},
}


def test_declared_draft4_schema_loads_and_scores_like_runtime(tmp_path):
    (tmp_path / "case.yaml").write_text(
        yaml.safe_dump({"schema": DRAFT4, "expected": {"number": 1}})
    )
    case = load_cases(tmp_path)[0]
    output = asyncio.run(Router(FakeModel([{"number": 1}])).generate("task", "s", [], DRAFT4))
    assert score_case(case, output.output).passed
    assert not score_case(case, {"number": 0}).passed


def test_declared_schema_dialect_still_rejects_invalid_schema(tmp_path):
    schema = {**DRAFT4, "definitions": {"positive": {"exclusiveMinimum": 1}}}
    (tmp_path / "case.yaml").write_text(yaml.safe_dump({"schema": schema}))
    with pytest.raises(jsonschema.SchemaError):
        load_cases(tmp_path)


def test_declared_dialect_does_not_bypass_private_case_integrity(tmp_path):
    (tmp_path / "case.yaml").write_text(
        yaml.safe_dump({"private": True, "schema": DRAFT4, "expected": {"number": 1}})
    )
    cases = load_cases(tmp_path)
    assert cases[0].provenance is not None and cases[0].provenance.kind == "private"
    local, cloud = FakeModel(), FakeModel()
    with pytest.raises(ValueError, match="owner review receipt"):
        asyncio.run(run_eval("task", "cloud", Router(local, cloud), cases))
    assert local.calls == cloud.calls == []
