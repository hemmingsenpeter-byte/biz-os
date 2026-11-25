"use client";

import type { ReactNode } from "react";
import { ShieldCheck } from "lucide-react";
import { useUser } from "./workspace";
import { Empty } from "./ui";
import { dateTime, readable, type Correction } from "@/lib/api";
import { trainingStatus, type TrainingCandidate } from "@/lib/training";

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
