export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/v1${path}`, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
      signal: init.signal ?? AbortSignal.timeout(20000),
    });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new ApiError(
      0,
      "We couldn't reach your workspace. Check the connection and try again.",
    );
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      typeof payload.detail === "string"
        ? payload.detail
        : `The request couldn't be completed (${response.status}).`,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}

export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

export type User = {
  id: string;
  email: string;
  role: string;
  display_name: string;
};
export type Approval = {
  id: string;
  created_at: string;
  actor: string;
  tool: string;
  inputs: Record<string, unknown>;
  version: number;
  approver: string;
  status: string;
  approved_by: string | null;
  reason: string;
  run_id: string | null;
  task_id: string | null;
  job_id: string | null;
  links: Record<string, string>;
};
export type Task = {
  id: string;
  job_id: string | null;
  title: string;
  kind: string;
  goal_key: string | null;
  due_at: string | null;
  assignee: string | null;
  status: string;
};
export type Activity = {
  id: string;
  kind: string;
  ref: string | null;
  task_id: string | null;
  job_id: string | null;
  at: string;
};

export type ControlState = {
  paused: boolean;
  by: string | null;
  reason: string;
  at: string | null;
};
export type Severity = "cosmetic" | "factual" | "policy";
export type Correction = {
  id: string;
  at: string;
  op_id: string;
  actor: string;
  tool: string;
  decision_type: string | null;
  from_version: number;
  to_version: number;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  by: string;
  reason: string;
  severity: string | null;
};
export type DecisionTypeRow = {
  role: string;
  decision_type: string;
  level: number | null;
  proposals: number;
  approved_unchanged: number;
  approved_cosmetic: number;
  approved_with_errors: number;
  approved_unknown: number;
  rejected: number;
  pending: number;
  approval_without_edit_rate: number | null;
  error_free_rate: number | null;
};
export type Metrics = {
  since: string;
  until: string;
  decision_types: DecisionTypeRow[];
  serious_errors: number | null;
  recorded_demotions: number;
  tasks_completed: number;
  cost_usd: number;
  cost_per_task_usd: number | null;
  time_saved_minutes: number | null;
};

// The API answers 409 with a detail starting "paused" while everything is paused.
export const isPausedError = (error: unknown) =>
  error instanceof ApiError &&
  error.status === 409 &&
  error.message.startsWith("paused");
export const SEVERITIES: { value: Severity; label: string; hint: string }[] = [
  {
    value: "cosmetic",
    label: "Wording or tone only",
    hint: "The content was right. You only changed how it reads.",
  },
  {
    value: "factual",
    label: "A fact was wrong",
    hint: "A name, date, amount or other detail was incorrect or missing.",
  },
  {
    value: "policy",
    label: "It broke one of our rules",
    hint: "It offered or did something we don't allow.",
  },
];
// The API records decisions by the owner under the name "owner".
export const person = (name: string | null) =>
  !name ? "someone" : name === "owner" ? "the owner" : name;

export const readable = (text: string) =>
  text.replaceAll(/[._:-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
export function dateTime(value: string | null): string {
  if (!value) return "Not set";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}
