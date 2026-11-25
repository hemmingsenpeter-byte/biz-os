"use client";

import Link from "next/link";
import { ArrowRight, RefreshCw } from "lucide-react";
import { useData } from "@/lib/use-data";
import { dateTime, readable } from "@/lib/api";
import type { TrainingCandidate, TrainingCorrection } from "@/lib/training";
import {
  CandidateStatus,
  TrainingAccess,
  TrainingBoundary,
} from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";

function OwnerTraining() {
  const corrections = useData<TrainingCorrection[]>("/training/corrections");
  const candidates = useData<TrainingCandidate[]>("/training/candidates");
  const loading = corrections.loading || candidates.loading;
  return (
    <>
      <PageHeading
        eyebrow="LEARN FROM CORRECTIONS"
        title="Training"
        action={
          <button
            className="button button-secondary"
            disabled={loading}
            onClick={() => {
              void corrections.reload();
              void candidates.reload();
            }}
          >
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      >
        Turn an owner correction into a proposed rule and a regression test
        case.
      </PageHeading>
      <TrainingBoundary />
      {(corrections.error || candidates.error) && (
        <ErrorNotice
          retry={() => {
            void corrections.reload();
            void candidates.reload();
          }}
        >
          {corrections.error || candidates.error}
        </ErrorNotice>
      )}
      <section className="panel training-section">
        <div className="panel-heading">
          <div>
            <h2>Training proposals</h2>
            <p>
              Review the exact rule, source draft and expected result before
              accepting it for testing.
            </p>
          </div>
        </div>
        {candidates.loading && !candidates.data ? (
          <Loading label="Loading training proposals…" />
        ) : candidates.data?.length ? (
          <div
            className="table-wrap"
            role="region"
            aria-label="Training proposals"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  <th>Proposed rule</th>
                  <th>Created</th>
                  <th>Status</th>
                  <th>
                    <span className="sr-only">Open proposal</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {candidates.data.map((candidate) => (
                  <tr key={candidate.id}>
                    <td>
                      <Link
                        className="row-title training-rule-link"
                        href={`/training/${candidate.id}`}
                      >
                        {candidate.rule}
                      </Link>
                    </td>
                    <td className="date-cell">{dateTime(candidate.at)}</td>
                    <td>
                      <CandidateStatus candidate={candidate} />
                    </td>
                    <td>
                      <Link
                        className="table-action"
                        href={`/training/${candidate.id}`}
                        aria-label={`Review proposed rule: ${candidate.rule}`}
                      >
                        <ArrowRight size={18} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !candidates.error && (
            <Empty title="No training proposals yet">
              Choose a correction below to propose a rule.
            </Empty>
          )
        )}
      </section>
      <section className="panel training-section">
        <div className="panel-heading">
          <div>
            <h2>Owner corrections</h2>
            <p>
              Recorded edits retain their original values and the reason for the
              change.
            </p>
          </div>
        </div>
        {corrections.loading && !corrections.data ? (
          <Loading label="Loading corrections…" />
        ) : corrections.data?.length ? (
          <div
            className="table-wrap"
            role="region"
            aria-label="Owner corrections for training"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  <th>Correction</th>
                  <th>Role</th>
                  <th>Source draft</th>
                  <th>
                    <span className="sr-only">Open correction</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {corrections.data.map((entry) => (
                  <tr key={entry.correction.id}>
                    <td>
                      <Link
                        className="row-title"
                        href={
                          entry.candidate_id
                            ? `/training/${entry.candidate_id}`
                            : `/training/corrections/${entry.correction.id}`
                        }
                      >
                        {entry.correction.reason ||
                          "Review recorded correction"}
                      </Link>
                      <span className="row-subtitle">
                        {dateTime(entry.correction.at)}
                      </span>
                    </td>
                    <td>{readable(entry.correction.actor)}</td>
                    <td>
                      {entry.snapshots.length
                        ? `${entry.snapshots.length} recorded ${entry.snapshots.length === 1 ? "draft" : "drafts"}`
                        : "Incomplete provenance"}
                    </td>
                    <td>
                      <Link
                        className="table-action"
                        href={
                          entry.candidate_id
                            ? `/training/${entry.candidate_id}`
                            : `/training/corrections/${entry.correction.id}`
                        }
                        aria-label={`Open correction: ${entry.correction.reason || entry.correction.id}`}
                      >
                        <ArrowRight size={18} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !corrections.error && (
            <Empty title="No corrections recorded">
              Owner edits will appear here with their reason and source history.
            </Empty>
          )
        )}
      </section>
    </>
  );
}

export default function TrainingPage() {
  return (
    <TrainingAccess>
      <OwnerTraining />
    </TrainingAccess>
  );
}
