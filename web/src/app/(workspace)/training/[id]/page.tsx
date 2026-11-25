"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Check, RefreshCw, X } from "lucide-react";
import {
  api,
  ApiError,
  dateTime,
  errorMessage,
  isPausedError,
  person,
  readable,
} from "@/lib/api";
import { useData } from "@/lib/use-data";
import type { TrainingCandidate, TrainingCorrection } from "@/lib/training";
import {
  CandidateStatus,
  CorrectionContext,
  TrainingAccess,
  TrainingBoundary,
  TrainingValue,
} from "@/components/training";
import { ErrorNotice, Loading, PageHeading } from "@/components/ui";
import { useControl, useUser } from "@/components/workspace";

function SourceCorrection({ id }: { id: string }) {
  const { data, loading, error, reload } = useData<TrainingCorrection>(
    `/training/corrections/${id}`,
  );
  return loading && !data ? (
    <Loading label="Loading source correction…" />
  ) : error ? (
    <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>
  ) : (
    data && <CorrectionContext correction={data.correction} />
  );
}

function OwnerCandidate({ id }: { id: string }) {
  const {
    data: candidate,
    loading,
    error,
    reload,
  } = useData<TrainingCandidate>(
    `/training/candidates/${encodeURIComponent(id)}`,
  );
  const router = useRouter();
  const control = useControl();
  const user = useUser();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [notice, setNotice] = useState("");
  const [reason, setReason] = useState("");
  const paused = Boolean(control.state?.paused);
  const who = (identity: string) =>
    identity === user?.id
      ? "you"
      : /^[0-9a-f-]{36}$/i.test(identity)
        ? "an owner"
        : person(identity);
  async function decide(action: "review" | "reject") {
    if (!candidate || busy) return;
    setBusy(true);
    setProblem("");
    setNotice("");
    try {
      await api<TrainingCandidate>(
        `/training/candidates/${candidate.id}/${action}`,
        { method: "POST", body: JSON.stringify({ reason: reason.trim() }) },
      );
      setNotice(
        action === "review"
          ? "Accepted for testing. No instructions have been deployed."
          : "Proposal rejected. Your decision is recorded.",
      );
      await reload();
    } catch (failure) {
      if (isPausedError(failure)) {
        setProblem(
          "Everything is paused. Resume before accepting a proposal for testing.",
        );
        control.refresh();
      } else if (failure instanceof ApiError && failure.status === 401)
        router.replace("/auth/login");
      else if (failure instanceof ApiError && failure.status === 409) {
        setProblem(
          "This proposal can no longer be reviewed in its current state. The latest record is shown below.",
        );
        await reload();
      } else setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Link className="back-link" href="/training">
        <ArrowLeft size={16} />
        Training
      </Link>
      {error && <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>}
      {loading && !candidate ? (
        <Loading label="Loading training proposal…" />
      ) : (
        candidate && (
          <>
            <PageHeading
              eyebrow="REVIEW A TRAINING PROPOSAL"
              title="Proposed rule"
              action={<CandidateStatus candidate={candidate} />}
            >
              Recorded on {dateTime(candidate.at)} by{" "}
              {who(candidate.created_by)}.
            </PageHeading>
            <TrainingBoundary />
            {(problem || notice) && (
              <div className="training-feedback">
                {problem && <ErrorNotice>{problem}</ErrorNotice>}
                {notice && (
                  <div className="notice notice-success" role="status">
                    {notice}
                  </div>
                )}
              </div>
            )}
            <div className="detail-grid">
              <div className="training-stack">
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Exact proposed rule</h2>
                    <button
                      className="icon-button"
                      aria-label="Refresh training proposal"
                      disabled={loading || busy}
                      onClick={() => void reload()}
                    >
                      <RefreshCw size={17} />
                    </button>
                  </div>
                  <div className="training-content">
                    <TrainingValue value={candidate.rule} />
                  </div>
                </section>
                <SourceCorrection id={candidate.correction_id} />
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Regression case</h2>
                      <p>
                        The recorded input and the owner&apos;s expected correct
                        result.
                      </p>
                    </div>
                  </div>
                  <div className="training-content">
                    <p className="field-hint">
                      This is a private tuning case. Testing review does not
                      confirm a historical dataset or establish deployment
                      readiness.
                    </p>
                    {candidate.snapshot ? (
                      <>
                        <dl className="meta-list training-metadata">
                          <div>
                            <dt>Source task</dt>
                            <dd>{readable(candidate.snapshot.task)}</dd>
                          </div>
                          <div>
                            <dt>Source step</dt>
                            <dd>{candidate.snapshot.step_id}</dd>
                          </div>
                          <div>
                            <dt>Recorded model</dt>
                            <dd>
                              {candidate.snapshot.configured_model ||
                                "Not recorded"}
                            </dd>
                          </div>
                          <div>
                            <dt>Model revision</dt>
                            <dd>
                              {candidate.snapshot.model_revision ||
                                "Not recorded"}
                            </dd>
                          </div>
                        </dl>
                        <h3>Input treated as data</h3>
                        <TrainingValue
                          value={candidate.snapshot.untrusted_text}
                        />
                        <h3>Expected result</h3>
                        <TrainingValue value={candidate.expected_output} />
                        <details className="training-source">
                          <summary>Recorded draft and schema</summary>
                          <h3>Original output</h3>
                          <TrainingValue value={candidate.snapshot.output} />
                          <h3>Output schema</h3>
                          <TrainingValue
                            value={candidate.snapshot.output_schema}
                          />
                        </details>
                      </>
                    ) : (
                      <p className="training-reason">
                        No source draft was recorded for this proposal.
                      </p>
                    )}
                  </div>
                </section>
              </div>
              <aside className="detail-sidebar">
                <section className="decision-card">
                  <h2>
                    {candidate.status === "proposed"
                      ? "Your review"
                      : "Review recorded"}
                  </h2>
                  {!candidate.eligible_for_testing && (
                    <div className="training-incomplete">
                      <strong>Incomplete provenance</strong>
                      <ul>
                        {candidate.incomplete_reasons.map((item, index) => (
                          <li key={`${index}-${item}`}>{item}</li>
                        ))}
                      </ul>
                      <p>This proposal cannot be accepted for testing.</p>
                    </div>
                  )}
                  {candidate.status === "proposed" ? (
                    <>
                      <p>
                        Accept this exact proposal for testing, or reject it.
                        Live instructions will not change.
                      </p>
                      <label
                        className="field-label"
                        htmlFor="training-review-reason"
                      >
                        Review note <span>(optional)</span>
                      </label>
                      <textarea
                        id="training-review-reason"
                        rows={3}
                        maxLength={2000}
                        value={reason}
                        disabled={busy}
                        onChange={(event) => setReason(event.target.value)}
                      />
                      <button
                        className="button button-primary full-width"
                        disabled={
                          busy ||
                          loading ||
                          Boolean(error) ||
                          paused ||
                          !candidate.eligible_for_testing
                        }
                        onClick={() => void decide("review")}
                      >
                        <Check size={16} />
                        {busy ? "Recording…" : "Accept for testing"}
                      </button>
                      {paused && (
                        <p className="paused-note">
                          Everything is paused. Resume before accepting. You can
                          still reject this proposal.
                        </p>
                      )}
                      <button
                        className="button button-secondary full-width"
                        disabled={busy || loading || Boolean(error)}
                        onClick={() => void decide("reject")}
                      >
                        <X size={16} />
                        Reject proposal
                      </button>
                    </>
                  ) : (
                    <>
                      <p>
                        {candidate.status === "accepted_for_testing"
                          ? "The proposed rule and case were accepted for testing. Regression evaluation and deployment approval are still separate."
                          : "This proposal was rejected. No instructions were changed."}
                      </p>
                      {candidate.review && (
                        <p className="preserve-text">
                          Recorded by {who(candidate.review.by)} on{" "}
                          {dateTime(candidate.review.at)}.
                          {candidate.review.reason &&
                            ` ${candidate.review.reason}`}
                        </p>
                      )}
                    </>
                  )}
                </section>
              </aside>
            </div>
          </>
        )
      )}
    </>
  );
}

export default function TrainingCandidatePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <TrainingAccess>
      <OwnerCandidate id={id} />
    </TrainingAccess>
  );
}
