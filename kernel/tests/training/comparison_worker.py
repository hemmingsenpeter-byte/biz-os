"""Fake-only subprocess for durable comparison crash/replay tests."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from biz_kernel import db
from biz_kernel.training import cohorts, comparison
from biz_kernel.training.cli import _launch
from dbos import DBOS


def main() -> None:
    url, system_url, job_id, mode, marker_path = sys.argv[1:6]
    marker = Path(marker_path)
    db.configure(url)
    with db.session_scope() as session:
        setup = cohorts.prepare_execution(session, uuid.UUID(job_id))
    comparison._verify = lambda model: {
        key: model[key]
        for key in ("backend", "backend_version", "base_url", "name", "digest", "ollama_version")
    }
    real_client = httpx.AsyncClient

    def handle(request):  # type: ignore[no-untyped-def]
        payload = json.loads(request.content)
        side = (
            "baseline"
            if payload["messages"][0]["content"] == setup["baseline_system"]
            else "candidate"
        )
        with marker.with_suffix(".calls").open("a") as stream:
            stream.write(side + "\n")
        if mode == "crash" and side == "candidate":
            marker.write_text("crashed during candidate inference")
            os._exit(17)
        answer = {"body": "unsupported"} if side == "baseline" else setup["cases"][0]["expected"]
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(answer)},
                "prompt_eval_count": 8,
                "eval_count": 3,
            },
        )

    def factory(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("trust_env") is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    comparison.httpx.AsyncClient = factory  # type: ignore[misc]
    _launch(url, system_url)
    result = comparison.start_comparison(job_id).get_result()
    print("RESULT", json.dumps({"valid": result["valid"], "verdict": result["verdict"]}))
    DBOS.destroy(workflow_completion_timeout_sec=10)


if __name__ == "__main__":
    main()
