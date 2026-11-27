"use client";

import type { ReactNode } from "react";
import { ShieldCheck } from "lucide-react";
import { useUser } from "./workspace";
import { Empty } from "./ui";
import { dateTime, readable, type Correction } from "@/lib/api";
import {
  trainingStatus,
  versionStatus,
  type TrainingCandidate,
  type TrainingVersion,
  type TrainingCohort,
} from "@/lib/training";

export function TrainingAccess({ children }: { children: ReactNode }) {
  const user = useUser();
  return user?.role === "owner" ? (
    children
  ) : (
    <Empty title="Owner access required">
      Only the owner can review corrections and training proposals.
    </Empty>
  );
}

export function TrainingBoundary() {
  return (
    <div className="training-boundary">
      <ShieldCheck size={19} />
      <p>
        <strong>Proposals are for testing.</strong> Reviewing a rule does not
        deploy it or change live instructions, permissions or autonomy.
      </p>
    </div>
  );
}

export function CandidateStatus({
  candidate,
}: {
  candidate: TrainingCandidate;
}) {
  return (
    <span className={`badge badge-${candidate.status}`}>
      <span className="badge-dot" />
      {trainingStatus(candidate)}
    </span>
  );
}

export function TrainingValue({ value }: { value: unknown }) {
  return (
    <pre className="training-value">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function CorrectionContext({ correction }: { correction: Correction }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Source correction</h2>
          <p>
            {readable(correction.actor)} · {dateTime(correction.at)} · Version{" "}
            {correction.from_version} to {correction.to_version}
          </p>
        </div>
      </div>
      <div className="training-content">
        <p className="training-reason">
          {correction.reason || "No reason was recorded for this correction."}
        </p>
        <div className="training-comparison">
          <div>
            <h3>Before the edit</h3>
            <TrainingValue value={correction.before} />
          </div>
          <div>
            <h3>After the edit</h3>
            <TrainingValue value={correction.after} />
          </div>
        </div>
      </div>
    </section>
  );
}

export function VersionStatus({ version }: { version: TrainingVersion }) {
  return (
    <span className={`badge badge-${version.status}`}>
      {versionStatus(version)}
    </span>
  );
}

export function VersionRecord({ version }: { version: TrainingVersion }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Version {version.number} record</h2>
      </div>
      <div className="training-content">
        <dl className="meta-list training-metadata">
          <div>
            <dt>Recorded</dt>
            <dd>{dateTime(version.at)}</dd>
          </div>
          <div>
            <dt>Task</dt>
            <dd>{version.task || "Not recorded"}</dd>
          </div>
          <div>
            <dt>Parent version</dt>
            <dd>{version.parent_id || "Original version"}</dd>
          </div>
          <div>
            <dt>Source draft</dt>
            <dd>{version.snapshot_id || "Not recorded"}</dd>
          </div>
        </dl>
        <h3>Proposed rule</h3>
        <TrainingValue value={version.rule} />
        <h3>Candidate instructions</h3>
        <TrainingValue value={version.system} />
        <h3>Expected target result</h3>
        <TrainingValue value={version.expected_output} />
        <details className="training-source">
          <summary>Frozen baseline, schema, checks and content hash</summary>
          <h3>Recorded baseline instructions</h3>
          <TrainingValue value={version.baseline_system} />
          <h3>Output schema</h3>
          <TrainingValue value={version.schema} />
          <h3>Checks</h3>
          <TrainingValue value={version.checks} />
          <h3>Version content hash</h3>
          <TrainingValue value={version.payload_sha256} />
        </details>
        {version.review && (
          <div className="training-incomplete">
            <strong>{versionStatus(version)}</strong>
            <p>
              Recorded on {dateTime(version.review.at)}. {version.review.reason}
            </p>
            <p>This review applies only to version {version.number}.</p>
          </div>
        )}
      </div>
    </section>
  );
}

export function FrozenCohort({ cohort }: { cohort: TrainingCohort }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>{cohort.name}</h2>
          <p>
            {cohort.members.length} private tuning cases · {cohort.task}
          </p>
        </div>
      </div>
      <div className="training-content">
        <p className="field-hint">
          Each case keeps its recorded input, expected result and checks.
          Held-out cases are excluded from training comparisons.
        </p>
        {cohort.members.map((member) => (
          <details className="training-source" key={member.case_id}>
            <summary>Case {member.case_id}</summary>
            <h3>Private input treated as data</h3>
            <TrainingValue value={member.untrusted} />
            <h3>Expected result</h3>
            <TrainingValue value={member.expected} />
            <h3>Checks</h3>
            <TrainingValue value={member.checks} />
            <h3>Case provenance</h3>
            <TrainingValue value={member} />
          </details>
        ))}
        <details className="training-source">
          <summary>Frozen cohort hash and schema</summary>
          <TrainingValue
            value={{
              cohort: cohort.id,
              content_sha256: cohort.payload_sha256,
              schema_sha256: cohort.schema_sha256,
            }}
          />
        </details>
        {cohort.review && (
          <p className="preserve-text">
            Owner attestation recorded on {dateTime(cohort.review.at)}.{" "}
            {cohort.review.reason}
          </p>
        )}
      </div>
    </section>
  );
}
