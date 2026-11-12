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
