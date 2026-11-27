"use client";

import Link from "next/link";
import { use, useState } from "react";
import { api, ApiError, errorMessage, isPausedError } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type {
  TrainingCandidate,
  TrainingComparison,
  TrainingVersion,
} from "@/lib/training";
import {
  TrainingAccess,
  TrainingBoundary,
  VersionRecord,
  VersionStatus,
} from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";
import { useControl } from "@/components/workspace";
import VersionEditor from "../editor";
import CohortForm from "./cohort";

function OwnerVersion({ id, versionId }: { id: string; versionId: string }) {
  const version = useData<TrainingVersion>(
    `/training/versions/${encodeURIComponent(versionId)}`,
  );
  const candidate = useData<TrainingCandidate>(
    `/training/candidates/${encodeURIComponent(id)}`,
  );
  const history = useData<TrainingVersion[]>(
    `/training/candidates/${encodeURIComponent(id)}/versions`,
  );
  const comparisons = useData<TrainingComparison[]>("/training/comparisons");
  const control = useControl();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [notice, setNotice] = useState("");
  const latest =
    [...(history.data ?? [])].sort((a, b) => b.number - a.number)[0] ?? null;
  async function decide(action: "review" | "reject") {
    if (!version.data || busy) return;
    setBusy(true);
    setProblem("");
    setNotice("");
    try {
      await api(`/training/versions/${version.data.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() }),
      });
      setNotice(
        action === "review"
          ? "This exact version is accepted for testing. No live instructions changed."
          : "This version is rejected. Its history stays available.",
      );
      await Promise.all([version.reload(), history.reload()]);
    } catch (failure) {
      if (isPausedError(failure)) {
        control.refresh();
        setProblem(
          "Everything is paused. Resume before accepting a version for testing.",
        );
      } else if (failure instanceof ApiError && failure.status === 409) {
        setProblem(
          "A review is already recorded for this exact version. Your note is kept; refresh and inspect the recorded decision.",
        );
        await version.reload();
      } else setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  if (version.error || candidate.error || history.error)
    return (
      <ErrorNotice
        retry={() => {
          void version.reload();
          void candidate.reload();
          void history.reload();
        }}
      >
        {version.error || candidate.error || history.error}
      </ErrorNotice>
    );
  if (!version.data || !candidate.data || !history.data)
    return <Loading label="Loading candidate version…" />;
  if (version.data.candidate_id !== candidate.data.id)
    return (
      <Empty title="Version does not belong to this proposal">
        Open the version from its own proposal history.
      </Empty>
    );
  const record = version.data;
  return (
    <>
      <Link className="back-link" href={`/training/${id}/versions`}>
        Version history
      </Link>
      <PageHeading
        eyebrow="IMMUTABLE TRAINING VERSION"
        title={`Candidate version ${record.number}`}
        action={<VersionStatus version={record} />}
      >
        Review this version independently. Earlier reviews do not carry over to
        a successor.
      </PageHeading>
      <TrainingBoundary />
      <div className="training-stack">
        <VersionRecord version={record} />
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
        {notice && (
          <p className="notice notice-success" role="status">
            {notice}
          </p>
        )}
        {record.status === "proposed" && (
          <section className="panel training-form">
            <div className="panel-heading">
              <h2>Review this exact version</h2>
            </div>
            <div className="training-content">
              {!record.eligible_for_testing && (
                <div className="training-incomplete">
                  <strong>Incomplete provenance</strong>
                  <ul>
                    {record.incomplete_reasons.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              <label className="field-label" htmlFor="version-review-reason">
                Version review note
              </label>
              <textarea
                id="version-review-reason"
                rows={3}
                value={reason}
                maxLength={2000}
                disabled={busy}
                onChange={(event) => setReason(event.target.value)}
              />
              <div className="edit-actions">
                <button
                  className="button button-secondary"
                  disabled={busy}
                  onClick={() => void decide("reject")}
                >
                  Reject version
                </button>
                <button
                  className="button button-primary"
                  disabled={
                    busy ||
                    !record.eligible_for_testing ||
                    Boolean(control.state?.paused)
                  }
                  onClick={() => void decide("review")}
                >
                  Accept version for testing
                </button>
              </div>
            </div>
          </section>
        )}
        <details className="panel training-content">
          <summary>Create a successor without rewriting this version</summary>
          <VersionEditor
            key={record.id}
            candidate={candidate.data}
            source={record}
            latest={latest}
            refresh={history.reload}
          />
        </details>
        {record.status === "accepted_for_testing" ? (
          <CohortForm key={record.id} version={record} />
        ) : (
          <Empty title="Comparison requires version-specific acceptance">
            Rejected, unreviewed and incomplete versions cannot run. A successor
            needs its own owner testing review.
          </Empty>
        )}
        <section className="panel">
          <div className="panel-heading">
            <h2>Comparisons for this version</h2>
            <button
              className="button button-secondary"
              onClick={() => void comparisons.reload()}
            >
              Refresh jobs
            </button>
          </div>
          <div className="training-content">
            {comparisons.error ? (
              <ErrorNotice retry={() => void comparisons.reload()}>
                {comparisons.error}
              </ErrorNotice>
            ) : comparisons.loading && !comparisons.data ? (
              <Loading label="Loading comparison jobs…" />
            ) : (comparisons.data ?? []).filter(
                (job) => job.version_id === record.id,
              ).length ? (
              <ul>
                {comparisons.data
                  ?.filter((job) => job.version_id === record.id)
                  .map((job) => (
                    <li key={job.id}>
                      <Link href={`/training/comparisons/${job.id}`}>
                        Comparison {job.id}
                      </Link>{" "}
                      · {job.status}
                    </li>
                  ))}
              </ul>
            ) : (
              <p>No comparison jobs for this version.</p>
            )}
          </div>
        </section>
      </div>
    </>
  );
}

export default function VersionPage({
  params,
}: {
  params: Promise<{ id: string; versionId: string }>;
}) {
  const { id, versionId } = use(params);
  return (
    <TrainingAccess>
      <OwnerVersion key={versionId} id={id} versionId={versionId} />
    </TrainingAccess>
  );
}
