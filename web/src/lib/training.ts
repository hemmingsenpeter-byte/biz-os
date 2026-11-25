import type { Correction } from "./api";

export type DraftSnapshot = {
  id: string;
  at: string;
  run_id: string;
  step_id: string;
  actor: string;
  task: string;
  prompt_path: string;
  system: string;
  output_schema: Record<string, unknown>;
  untrusted_text: string;
  output: Record<string, unknown>;
  tier: string;
  backend: string;
  configured_model: string | null;
  model_revision: string | null;
  [key: `${string}_sha256`]: string;
};

export type TrainingCorrection = {
  correction: Correction;
  snapshots: DraftSnapshot[];
  candidate_id: string | null;
};

export type TrainingCandidate = {
  id: string;
  at: string;
  correction_id: string;
  snapshot_id: string | null;
  rule: string;
  expected_output: Record<string, unknown> | null;
  regression_case: Record<string, unknown> | null;
  created_by: string;
  payload_sha256: string;
  status: "proposed" | "accepted_for_testing" | "rejected";
  eligible_for_testing: boolean;
  incomplete_reasons: string[];
  snapshot: DraftSnapshot | null;
  review: {
    id: string;
    at: string;
    candidate_id: string;
    decision: "accepted_for_testing" | "rejected";
    by: string;
    reason: string;
  } | null;
  readiness_established: false;
};

export const trainingStatus = (candidate: TrainingCandidate) =>
  candidate.status === "accepted_for_testing"
    ? "Accepted for testing"
    : candidate.status === "rejected"
      ? "Rejected"
      : candidate.eligible_for_testing
        ? "Needs review"
        : "Incomplete";
