"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Check } from "lucide-react";
import { api, ApiError, dateTime, errorMessage } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type { TrainingCandidate, TrainingCorrection } from "@/lib/training";
import {
  CorrectionContext,
  TrainingAccess,
  TrainingBoundary,
  TrainingValue,
} from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";

function ProposalForm({ entry }: { entry: TrainingCorrection }) {
  const router = useRouter();
  const [rule, setRule] = useState("");
  const [snapshotId, setSnapshotId] = useState("");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const snapshot = entry.snapshots.find((item) => item.id === snapshotId);
  async function submit() {
    if (busy) return;
    let expectedOutput: Record<string, unknown> | null = null;
    if (!rule.trim()) {
      setProblem("Write the proposed rule before saving.");
      return;
    }
    if (snapshot) {
      try {
        const parsed: unknown = JSON.parse(expected);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
          throw new Error();
        expectedOutput = parsed as Record<string, unknown>;
      } catch {
        setProblem(
          "Enter the expected result as a JSON object. The selected draft's schema will also be checked.",
        );
        return;
      }
    }
    setBusy(true);
    setProblem("");
    try {
      const saved = await api<TrainingCandidate>(
        `/training/corrections/${entry.correction.id}/candidates`,
        {
          method: "POST",
          body: JSON.stringify({
            rule: rule.trim(),
            snapshot_id: snapshot?.id ?? null,
            expected_output: expectedOutput,
          }),
        },
      );
      router.push(`/training/${saved.id}`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401)
        router.replace("/auth/login");
      else if (error instanceof ApiError && error.status === 409)
        setProblem(
          "A different proposal already exists for this correction. Your unsaved rule and expected result are kept. Refresh to open the recorded proposal.",
        );
      else setProblem(errorMessage(error));
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
        void submit();
      }}
    >
      <div className="panel-heading">
        <div>
          <h2>Propose a rule</h2>
          <p>Record the rule and the correct result for one regression case.</p>
        </div>
      </div>
      <div className="training-content">
        <label className="field-label" htmlFor="training-rule">
          Proposed rule
        </label>
        <textarea
          id="training-rule"
          rows={4}
          maxLength={4000}
          value={rule}
          onChange={(event) => setRule(event.target.value)}
          disabled={busy}
          placeholder="What should the employee do differently next time?"
        />
        <label className="field-label" htmlFor="training-snapshot">
          Recorded source draft
        </label>
        <select
          id="training-snapshot"
          value={snapshotId}
          disabled={busy || !entry.snapshots.length}
          onChange={(event) => {
            setSnapshotId(event.target.value);
            setExpected("");
            setProblem("");
          }}
        >
          <option value="">
            No matching draft — save an incomplete proposal
          </option>
          {entry.snapshots.map((item) => (
            <option key={item.id} value={item.id}>
              {item.task} · {item.step_id} · {dateTime(item.at)}
            </option>
          ))}
        </select>
        {!snapshot ? (
          <div className="training-incomplete" role="status">
            <strong>Incomplete provenance</strong>
            <p>
              A rule without a recorded source draft cannot be accepted for
              testing. Its history will remain incomplete.
            </p>
          </div>
        ) : (
          <>
            <h3>Recorded output</h3>
            <TrainingValue value={snapshot.output} />
            <p className="field-hint">
              This is the output before the correction. Enter the expected
              correct result below; the tool-input edit is not assumed to be a
              model result.
            </p>
            <label className="field-label" htmlFor="training-expected">
              Expected result (JSON)
            </label>
            <textarea
              className="training-json-editor"
              id="training-expected"
              rows={9}
              value={expected}
              onChange={(event) => setExpected(event.target.value)}
              disabled={busy}
              spellCheck={false}
              placeholder={
                '{\n  "subject": "Correct subject",\n  "body": "Correct reply",\n  "confidence": 0.95\n}'
              }
            />
            <details className="training-source">
              <summary>Recorded input and output schema</summary>
              <h3>Input treated as data</h3>
              <TrainingValue value={snapshot.untrusted_text} />
              <h3>Required output schema</h3>
              <TrainingValue value={snapshot.output_schema} />
            </details>
          </>
        )}
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
        <div className="edit-actions">
          <Link className="button button-quiet" href="/training">
            Cancel
          </Link>
          <button
            className="button button-primary"
            type="submit"
            disabled={busy}
          >
            <Check size={16} />
            {busy ? "Saving…" : "Create training proposal"}
          </button>
        </div>
        <p className="field-hint">
          Saving records a proposal. It does not change any deployed
          instructions.
        </p>
      </div>
    </form>
  );
}

function OwnerCorrection({ id }: { id: string }) {
  const { data, loading, error, reload } = useData<TrainingCorrection>(
    `/training/corrections/${encodeURIComponent(id)}`,
  );
  return (
    <>
      <Link className="back-link" href="/training">
        <ArrowLeft size={16} />
        Training
      </Link>
      <PageHeading
        eyebrow="CORRECTION TO REGRESSION CASE"
        title="New training proposal"
      >
        A proposal keeps the source correction and the draft it tests together.
      </PageHeading>
      <TrainingBoundary />
      {error && <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>}
      {loading && !data ? (
        <Loading label="Loading source correction…" />
      ) : (
        data &&
        (data.candidate_id ? (
          <section className="panel">
            <Empty title="A proposal is already recorded">
              Each correction has one immutable proposal.
            </Empty>
            <div className="panel-bottom">
              <Link
                className="button button-primary"
                href={`/training/${data.candidate_id}`}
              >
                Open recorded proposal
              </Link>
            </div>
          </section>
        ) : (
          <div className="training-stack">
            <CorrectionContext correction={data.correction} />
            <ProposalForm key={data.correction.id} entry={data} />
          </div>
        ))
      )}
    </>
  );
}

export default function TrainingCorrectionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <TrainingAccess>
      <OwnerCorrection id={id} />
    </TrainingAccess>
  );
}
