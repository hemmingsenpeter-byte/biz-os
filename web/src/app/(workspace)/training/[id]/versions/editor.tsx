"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, errorMessage } from "@/lib/api";
import {
  jsonObject,
  type TrainingCandidate,
  type TrainingVersion,
} from "@/lib/training";
import { TrainingValue } from "@/components/training";
import { ErrorNotice } from "@/components/ui";

export default function VersionEditor({
  candidate,
  source,
  latest,
  refresh,
}: {
  candidate: TrainingCandidate;
  source: TrainingVersion | null;
  latest: TrainingVersion | null;
  refresh: () => Promise<unknown>;
}) {
  const router = useRouter();
  const [parentId, setParentId] = useState(source?.id ?? null);
  const [rule, setRule] = useState(source?.rule ?? candidate.rule);
  const [system, setSystem] = useState(
    source?.system ??
      (candidate.snapshot
        ? `${candidate.snapshot.system}\n\nProposed rule:\n${candidate.rule}`
        : ""),
  );
  const [expected, setExpected] = useState(
    JSON.stringify(
      source ? source.expected_output : candidate.expected_output,
      null,
      2,
    ),
  );
  const [checks, setChecks] = useState(
    JSON.stringify(
      source?.checks ??
        candidate.regression_case?.checks ?? [{ type: "json_schema" }],
      null,
      2,
    ),
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [conflict, setConflict] = useState(false);
  const stale = latest ? latest.id !== parentId : parentId !== null;
  async function save() {
    if (busy) return;
    let expectedOutput: Record<string, unknown> | null;
    let parsedChecks: Record<string, unknown>[];
    try {
      expectedOutput =
        expected.trim() === "null"
          ? null
          : jsonObject(expected, "Expected result");
      const value: unknown = JSON.parse(checks);
      if (
        !Array.isArray(value) ||
        value.some(
          (check) =>
            !check || typeof check !== "object" || Array.isArray(check),
        )
      )
        throw new Error("Checks must be a JSON list of check objects.");
      parsedChecks = value as Record<string, unknown>[];
      if (!rule.trim() || !system.trim())
        throw new Error("Enter the proposed rule and candidate instructions.");
    } catch (failure) {
      setProblem(errorMessage(failure));
      return;
    }
    setBusy(true);
    setProblem("");
    try {
      const version = await api<TrainingVersion>(
        `/training/candidates/${candidate.id}/versions`,
        {
          method: "POST",
          body: JSON.stringify({
            parent_id: parentId,
            rule: rule.trim(),
            system,
            expected_output: expectedOutput,
            snapshot_id: source ? source.snapshot_id : candidate.snapshot_id,
            checks: parsedChecks,
          }),
        },
      );
      router.push(`/training/${candidate.id}/versions/${version.id}`);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        setConflict(true);
        setProblem(
          "A newer version was saved. Your unsaved rule, instructions and expected result are kept. Review the latest version before choosing it as the parent.",
        );
        await refresh();
      } else setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="panel training-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div className="panel-heading">
        <h2>
          {source || latest
            ? "Create a successor version"
            : "Create the first version"}
        </h2>
      </div>
      <div className="training-content">
        <p>
          Existing versions and reviews stay unchanged. Every new version needs
          its own testing review.
        </p>
        <p className="field-hint">Parent: {parentId || "Original proposal"}</p>
        {(stale || conflict) && latest && latest.id !== parentId && (
          <div className="training-incomplete">
            <strong>
              A newer parent is available: version {latest.number}
            </strong>
            <details className="training-source">
              <summary>Review latest version before keeping your edits</summary>
              <h3>Latest rule</h3>
              <TrainingValue value={latest.rule} />
              <h3>Latest instructions</h3>
              <TrainingValue value={latest.system} />
              <h3>Latest expected result</h3>
              <TrainingValue value={latest.expected_output} />
              <button
                className="button button-secondary"
                type="button"
                disabled={busy}
                onClick={() => {
                  setParentId(latest.id);
                  setConflict(false);
                  setProblem("");
                }}
              >
                Use version {latest.number} as parent
              </button>
            </details>
          </div>
        )}
        <label className="field-label" htmlFor="version-rule">
          Version rule
        </label>
        <textarea
          id="version-rule"
          rows={4}
          value={rule}
          maxLength={8000}
          disabled={busy}
          onChange={(event) => setRule(event.target.value)}
        />
        <label className="field-label" htmlFor="version-system">
          Candidate instructions
        </label>
        <textarea
          id="version-system"
          rows={10}
          value={system}
          disabled={busy}
          onChange={(event) => setSystem(event.target.value)}
        />
        <label className="field-label" htmlFor="version-expected">
          Expected target result (JSON)
        </label>
        <textarea
          id="version-expected"
          className="training-json-editor"
          rows={8}
          value={expected}
          disabled={busy}
          spellCheck={false}
          onChange={(event) => setExpected(event.target.value)}
        />
        <details className="training-source">
          <summary>Exact scoring checks</summary>
          <label className="field-label" htmlFor="version-checks">
            Checks (JSON list)
          </label>
          <textarea
            id="version-checks"
            className="training-json-editor"
            rows={8}
            value={checks}
            disabled={busy}
            spellCheck={false}
            onChange={(event) => setChecks(event.target.value)}
          />
        </details>
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
        <button
          className="button button-primary"
          type="submit"
          disabled={busy || stale}
        >
          {busy ? "Saving version…" : "Save new version"}
        </button>
      </div>
    </form>
  );
}
