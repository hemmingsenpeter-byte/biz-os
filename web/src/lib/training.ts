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

export type TestingReview = {
  id: string;
  at: string;
  decision: "accepted_for_testing" | "rejected";
  by: string;
  reason: string;
};

export type TrainingVersion = {
  id: string;
  at: string;
  candidate_id: string;
  number: number;
  parent_id: string | null;
  snapshot_id: string | null;
  rule: string;
  system: string;
  baseline_system: string | null;
  expected_output: Record<string, unknown> | null;
  checks: Record<string, unknown>[];
  created_by: string;
  payload_sha256: string;
  task: string | null;
  schema: Record<string, unknown> | null;
  status: "proposed" | "accepted_for_testing" | "rejected";
  eligible_for_testing: boolean;
  incomplete_reasons: string[];
  review: TestingReview | null;
  readiness_established: false;
};

export type CohortMember = {
  case_id: string;
  version_id: string;
  version_sha256: string;
  snapshot_id: string;
  untrusted: unknown;
  expected: Record<string, unknown>;
  checks: Record<string, unknown>[];
  [key: string]: unknown;
};

export type TrainingCohort = {
  id: string;
  at: string;
  name: string;
  task: string;
  schema_sha256: string;
  members: CohortMember[];
  payload_sha256: string;
  status: "proposed" | "accepted_for_testing";
  review: {
    id: string;
    at: string;
    cohort_id: string;
    cohort_sha256: string;
    by: string;
    reason: string;
    payload_sha256: string;
  } | null;
  readiness_established: false;
};

export type ComparisonModel = {
  name: string;
  digest: string;
  ollama_version: string;
  backend_version: string;
  base_url: string;
  temperature: number;
  seed: number;
  num_predict: number;
  timeout_seconds: number;
  repetitions: number;
};

export type ComparisonCaseResult = {
  side: "baseline" | "candidate";
  case_id: string;
  repetition: number;
  status: "completed" | "error" | "unknown";
  passed?: boolean;
  score?: number;
  failures?: string[];
  output?: Record<string, unknown> | null;
  error?: string | null;
  latency_ms?: number | null;
  usage?: Record<string, unknown> | null;
  api_cost_usd?: string | number | null;
  hardware_cost_usd?: null;
  [key: string]: unknown;
};

export type ComparisonEvent = {
  id: string;
  at: string;
  kind: string;
  payload: Record<string, unknown>;
};

export type TrainingComparison = {
  id: string;
  at: string;
  version_id: string;
  cohort_id: string;
  status: "queued" | "running" | "completed" | "failed" | "unknown";
  contract: Record<string, unknown>;
  events: ComparisonEvent[];
  summary: Record<string, unknown> | null;
  readiness_established: false;
};

export const versionStatus = (version: TrainingVersion) =>
  version.status === "accepted_for_testing"
    ? "Accepted for testing"
    : version.status === "rejected"
      ? "Rejected"
      : version.eligible_for_testing
        ? "Needs version review"
        : "Incomplete provenance";

export function jsonObject(text: string, label: string) {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    throw new Error(`${label} must be a JSON object.`);
  return parsed as Record<string, unknown>;
}
