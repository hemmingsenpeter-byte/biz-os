"use client";

import Link from "next/link";
import { dateTime, readable } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type { TrainingCohort, TrainingComparison } from "@/lib/training";
import { TrainingAccess, TrainingBoundary } from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";

function OwnerComparisons() {
  const cohorts = useData<TrainingCohort[]>("/training/cohorts");
  const jobs = useData<TrainingComparison[]>("/training/comparisons");
  return (
    <>
      <Link className="back-link" href="/training">
        Training proposals
      </Link>
      <PageHeading
        eyebrow="PRIVATE TRAINING HISTORY"
        title="Cohorts and local comparisons"
      >
        Return to a frozen cohort, its owner attestation or a durable comparison
        job.
      </PageHeading>
      <TrainingBoundary />
      <div className="training-stack">
        <section className="panel">
          <div className="panel-heading">
            <h2>Frozen tuning cohorts</h2>
          </div>
          <div className="training-content">
            {cohorts.error ? (
              <ErrorNotice retry={() => void cohorts.reload()}>
                {cohorts.error}
              </ErrorNotice>
            ) : !cohorts.data ? (
              <Loading label="Loading cohorts…" />
            ) : cohorts.data.length ? (
              <ul>
                {cohorts.data.map((cohort) => (
                  <li key={cohort.id}>
                    <Link href={`/training/cohorts/${cohort.id}`}>
                      {cohort.name}
                    </Link>{" "}
                    · {cohort.members.length} cases ·{" "}
                    {cohort.status === "accepted_for_testing"
                      ? "Attested for tuning"
                      : "Needs attestation"}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty title="No frozen cohorts yet">
                Choose an accepted candidate version to freeze tuning cases.
              </Empty>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>Comparison jobs</h2>
          </div>
          <div className="training-content">
            {jobs.error ? (
              <ErrorNotice retry={() => void jobs.reload()}>
                {jobs.error}
              </ErrorNotice>
            ) : !jobs.data ? (
              <Loading label="Loading comparison history…" />
            ) : jobs.data.length ? (
              <ul>
                {jobs.data.map((job) => (
                  <li key={job.id}>
                    <Link href={`/training/comparisons/${job.id}`}>
                      Comparison {dateTime(job.at)}
                    </Link>{" "}
                    · {readable(job.status)}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty title="No comparisons queued">
                Review and attest a frozen tuning cohort before queueing a
                comparison.
              </Empty>
            )}
          </div>
        </section>
      </div>
    </>
  );
}

export default function ComparisonsPage() {
  return (
    <TrainingAccess>
      <OwnerComparisons />
    </TrainingAccess>
  );
}
