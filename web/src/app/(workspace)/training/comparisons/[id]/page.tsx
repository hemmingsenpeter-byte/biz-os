"use client";

import Link from "next/link";
import { use, useEffect } from "react";
import { dateTime, readable } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type { ComparisonCaseResult, TrainingComparison } from "@/lib/training";
import {
  TrainingAccess,
  TrainingBoundary,
  TrainingValue,
} from "@/components/training";
import { ErrorNotice, Loading, PageHeading } from "@/components/ui";

const terminal = new Set(["completed", "failed", "unknown"]);

function SideResult({ result }: { result: ComparisonCaseResult | undefined }) {
  if (!result)
    return (
      <p className="training-incomplete">
        Missing result. No outcome is established for this side.
      </p>
    );
  return (
    <div>
      <p>
        <strong>
          {result.status !== "completed"
            ? readable(result.status)
            : result.passed
              ? "Passed"
              : "Failed checks"}
        </strong>
      </p>
      {result.error && <p className="training-reason">{result.error}</p>}
      {result.failures?.length ? (
        <ul>
          {result.failures.map((failure, index) => (
            <li key={index}>{failure}</li>
          ))}
        </ul>
      ) : null}
      <p className="field-hint">
        Latency:{" "}
        {result.latency_ms == null
          ? "not measured"
          : `${result.latency_ms.toFixed(0)} ms`}{" "}
        · API cost:{" "}
        {result.api_cost_usd == null
          ? "not measured"
          : `$${result.api_cost_usd}`}
      </p>
      <h3>Recorded output</h3>
      <TrainingValue value={result.output ?? null} />
      <details className="training-source">
        <summary>Attempts, usage and model evidence</summary>
        <TrainingValue value={result} />
      </details>
    </div>
  );
}

function ComparisonEvidence({ job }: { job: TrainingComparison }) {
  const summary = job.summary;
  const results = Array.isArray(summary?.results)
    ? (summary.results as ComparisonCaseResult[])
    : [];
  const keys = [
    ...new Set(results.map((row) => `${row.case_id}:${row.repetition}`)),
  ];
  const regressions = Array.isArray(summary?.regressions)
    ? (summary.regressions as { case_id: string; repetition: number }[])
    : [];
  const missing = Array.isArray(summary?.missing)
    ? (summary.missing as { case_id: string; repetition: number }[])
    : [];
  for (const row of missing)
    if (!keys.includes(`${row.case_id}:${row.repetition}`))
      keys.push(`${row.case_id}:${row.repetition}`);
  return (
    <div className="training-stack">
      <section className="panel">
        <div className="panel-heading">
          <h2>Comparison outcome</h2>
        </div>
        <div className="training-content">
          {!summary ? (
            <>
              <p role="status">
                {job.status === "queued"
                  ? "Queued for the local host worker."
                  : job.status === "running"
                    ? "Comparison running. Results are provisional until both sides finish."
                    : "No completed summary is available. This comparison establishes no outcome."}
              </p>
              <p>
                Job progress is saved independently of this page. You can return
                to this link later.
              </p>
            </>
          ) : (
            <>
              <p role="status">
                <strong>
                  {summary.valid === true
                    ? `Tuning result: ${readable(String(summary.verdict ?? "not recorded"))}`
                    : "Invalid comparison — no result is established"}
                </strong>
              </p>
              <p>
                Baseline passed {String(summary.baseline_passed ?? "unknown")} /{" "}
                {String(summary.total_cases ?? "unknown")}; candidate passed{" "}
                {String(summary.candidate_passed ?? "unknown")} /{" "}
                {String(summary.total_cases ?? "unknown")}.
              </p>
              {regressions.length > 0 && (
                <div className="training-incomplete">
                  <strong>
                    {regressions.length} regression
                    {regressions.length === 1 ? " requires" : "s require"}{" "}
                    review
                  </strong>
                  <p>
                    A baseline pass becoming a candidate failure remains a
                    regression even when the total score improves.
                  </p>
                </div>
              )}
              <h3>Target case outcome</h3>
              <TrainingValue
                value={summary.target_outcomes ?? "Not recorded"}
              />
              <h3>Errors and missing or unknown outcomes</h3>
              <TrainingValue
                value={{
                  error: summary.error ?? null,
                  errors: summary.errors ?? [],
                  missing: summary.missing ?? [],
                }}
              />
              <p className="field-hint">
                Known model API cost:{" "}
                {summary.known_api_cost_usd == null
                  ? "not measured"
                  : `$${String(summary.known_api_cost_usd)}`}
                . Unmeasured API results:{" "}
                {String(summary.unmeasured_api_cost_results ?? "unknown")}.
                Hardware and electricity cost are not measured.
              </p>
            </>
          )}
          <p>
            This tuning comparison does not establish held-out readiness,
            production safety or time saved. It changes no deployed instruction
            or permission.
          </p>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Every case and repetition</h2>
        </div>
        <div className="training-content">
          {!keys.length && (
            <p>
              No paired case results have been recorded in a final summary yet.
              Inspect provisional events below.
            </p>
          )}
          {keys.map((key) => {
            const sample =
              results.find(
                (row) => `${row.case_id}:${row.repetition}` === key,
              ) ??
              missing.find(
                (row) => `${row.case_id}:${row.repetition}` === key,
              )!;
            const baseline = results.find(
              (row) =>
                row.side === "baseline" &&
                row.case_id === sample.case_id &&
                row.repetition === sample.repetition,
            );
            const candidate = results.find(
              (row) =>
                row.side === "candidate" &&
                row.case_id === sample.case_id &&
                row.repetition === sample.repetition,
            );
            const regression = regressions.some(
              (row) =>
                row.case_id === sample.case_id &&
                row.repetition === sample.repetition,
            );
            return (
              <section
                key={key}
                aria-label={`Case ${sample.case_id}, repetition ${sample.repetition}`}
              >
                <h3>
                  Case {sample.case_id} · Repetition {sample.repetition}
                  {regression ? " · Regression" : ""}
                </h3>
                <div className="training-comparison">
                  <div>
                    <h3>Fresh baseline</h3>
                    <SideResult result={baseline} />
                  </div>
                  <div>
                    <h3>Candidate</h3>
                    <SideResult result={candidate} />
                  </div>
                </div>
              </section>
            );
          })}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Frozen setup and provenance</h2>
        </div>
        <div className="training-content">
          <p>
            The job binds exact version, cohort, prompts, scorer, model digest,
            software versions and generation settings.
          </p>
          <TrainingValue value={job.contract} />
          <Link
            href={`/training/cohorts/${job.cohort_id}?version=${job.version_id}`}
          >
            Inspect the frozen cohort and expected checks
          </Link>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Recorded progress and provisional attempts</h2>
        </div>
        <div className="training-content">
          {job.events.length ? (
            job.events.map((event) => (
              <details className="training-source" key={event.id}>
                <summary>
                  {readable(event.kind)} · {dateTime(event.at)}
                </summary>
                <TrainingValue value={event.payload} />
              </details>
            ))
          ) : (
            <p>No worker events have been recorded yet.</p>
          )}
        </div>
      </section>
    </div>
  );
}

function OwnerComparison({ id }: { id: string }) {
  const job = useData<TrainingComparison>(
    `/training/comparisons/${encodeURIComponent(id)}`,
  );
  const { reload } = job;
  const active = Boolean(
    job.data && !job.error && !terminal.has(job.data.status),
  );
  const verified = Boolean(job.data && !job.error && !job.loading);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      await reload();
      if (!cancelled) timer = setTimeout(() => void poll(), 2000);
    }
    timer = setTimeout(() => void poll(), 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [active, reload]);
  return (
    <>
      <Link className="back-link" href="/training">
        Training
      </Link>
      <PageHeading
        eyebrow="PRIVATE LOCAL COMPARISON"
        title="Training comparison evidence"
        action={
          <button
            className="button button-secondary"
            disabled={job.loading}
            onClick={() => void reload()}
          >
            Refresh comparison
          </button>
        }
      >
        {job.error
          ? "Comparison evidence could not be verified."
          : verified && job.data
            ? `Job ${job.data.status} · ${dateTime(job.data.at)}`
            : "Verifying durable job evidence."}
      </PageHeading>
      <TrainingBoundary />
      {job.error && (
        <ErrorNotice retry={() => void reload()}>{job.error}</ErrorNotice>
      )}
      {job.error ? (
        <p role="status">
          Previously loaded results are hidden until a successful refresh
          verifies the evidence.
        </p>
      ) : !verified || !job.data ? (
        <Loading label="Verifying comparison evidence…" />
      ) : (
        <ComparisonEvidence job={job.data} />
      )}
    </>
  );
}

export default function ComparisonPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <TrainingAccess>
      <OwnerComparison id={id} />
    </TrainingAccess>
  );
}
