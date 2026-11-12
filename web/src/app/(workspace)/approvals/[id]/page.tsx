"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Check,
  FileText,
  Pencil,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  dateTime,
  errorMessage,
  isPausedError,
  readable,
  SEVERITIES,
  type Approval,
  type Severity,
} from "@/lib/api";
import { useData } from "@/lib/use-data";
import {
  Badge,
  DetailLink,
  ErrorNotice,
  Loading,
  PageHeading,
} from "@/components/ui";
import { useControl, useUser } from "@/components/workspace";

type Edit = {
  inputs: Record<string, unknown>;
  reason: string;
  severity: Severity;
};

function InputFields({ entries }: { entries: [string, unknown][] }) {
  return (
    <dl className="input-list">
      {entries.map(([key, value]) => (
        <div key={key} className="input-field">
          <dt>
            {readable(key)}
            <code>{key}</code>
          </dt>
          <dd>
            <pre>
              {typeof value === "string"
                ? value
                : JSON.stringify(value, null, 2)}
            </pre>
          </dd>
        </div>
      ))}
    </dl>
  );
}

// One line for a short value (it stays a single-line field); long text gets room.
function rowsFor(text: string) {
  const lines = text
    .split("\n")
    .reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / 60)), 0);
  return lines > 1 ? Math.min(16, lines + 2) : 1;
}

function EditForm({
  request,
  busy,
  onSave,
  onCancel,
}: {
  request: Approval;
  busy: boolean;
  onSave: (edit: Edit) => void;
  onCancel: () => void;
}) {
  const entries = Object.entries(request.inputs).filter(
    ([key]) => key !== "idempotency_key",
  );
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      entries.filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    ),
  );
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState<Severity | "">("");
  const [problem, setProblem] = useState("");
  const changed = Object.entries(draft).some(
    ([key, value]) => value !== request.inputs[key],
  );
  function submit() {
    if (!changed) setProblem("Change at least one value before saving.");
    else if (!reason.trim()) setProblem("Say in one line why you changed it.");
    else if (!severity) setProblem("Choose what kind of change this was.");
    else {
      setProblem("");
      // Everything is sent back; only the text values the owner typed differ.
      onSave({
        inputs: { ...request.inputs, ...draft },
        reason: reason.trim(),
        severity,
      });
    }
  }
  return (
    <form
      className="edit-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="input-list">
        {entries.map(([key, value]) => {
          const original = typeof value === "string" ? value : null;
          return (
            <div key={key} className="input-field">
              {original === null ? (
                <span className="input-label">
                  {readable(key)}
                  <code>{key}</code>
                </span>
              ) : (
                <label className="input-label" htmlFor={`edit-${key}`}>
                  {readable(key)}
                  <code>{key}</code>
                </label>
              )}
              {original === null ? (
                <>
                  <div className="input-value">
                    <pre>{JSON.stringify(value, null, 2)}</pre>
                  </div>
                  <p className="field-hint">
                    This value can&apos;t be changed here. It is kept as it is.
                  </p>
                </>
              ) : rowsFor(original) > 1 ? (
                <textarea
                  id={`edit-${key}`}
                  rows={rowsFor(original)}
                  value={draft[key]}
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, [key]: event.target.value })
                  }
                />
              ) : (
                <input
                  id={`edit-${key}`}
                  value={draft[key]}
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, [key]: event.target.value })
                  }
                />
              )}
            </div>
          );
        })}
      </div>
      <div className="edit-why">
        <h3>Why did you change it?</h3>
        <p>
          Your corrections are how this role learns. A short, honest answer
          helps it get the next one right.
        </p>
        <label className="field-label" htmlFor="edit-reason">
          Reason, in one line
        </label>
        <input
          id="edit-reason"
          value={reason}
          maxLength={300}
          disabled={busy}
          placeholder="What was wrong with the draft?"
          onChange={(event) => setReason(event.target.value)}
        />
        <fieldset className="severity-choice" disabled={busy}>
          <legend className="field-label">What kind of change was it?</legend>
          {SEVERITIES.map((option) => (
            <label key={option.value} className="severity-option">
              <input
                type="radio"
                name="severity"
                value={option.value}
                checked={severity === option.value}
                onChange={() => setSeverity(option.value)}
              />
              <span>
                <strong>{option.label}</strong>
                <small>{option.hint}</small>
              </span>
            </label>
          ))}
        </fieldset>
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
        <div className="edit-actions">
          <button
            className="button button-quiet"
            type="button"
            disabled={busy}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="button button-primary"
            type="submit"
            disabled={busy}
          >
            <Check size={16} />
            {busy ? "Saving…" : `Save as version ${request.version + 1}`}
          </button>
        </div>
        <p className="field-hint">
          Saving does not approve anything. You review the new version, then
          approve it.
        </p>
      </div>
    </form>
  );
}

export default function ApprovalPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const {
    data: request,
    loading,
    error,
    reload,
  } = useData<Approval>(`/approvals/${encodeURIComponent(id)}`);
  const user = useUser();
  const control = useControl();
  const paused = Boolean(control.state?.paused);
  const [editing, setEditing] = useState(false);
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [decisionError, setDecisionError] = useState("");
  const [notice, setNotice] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  async function decide(action: "approve" | "reject") {
    if (!request || busy) return;
    setBusy(true);
    setDecisionError("");
    setNotice("");
    try {
      await api<Approval>(`/approvals/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        body: JSON.stringify(
          action === "approve" ? { version: request.version } : { reason },
        ),
      });
      setRejecting(false);
      setNotice(
        action === "approve"
          ? `Version ${request.version} approved. The workflow can now continue.`
          : "Request rejected. Your decision has been recorded.",
      );
      await reload();
    } catch (err) {
      if (isPausedError(err)) {
        setDecisionError(
          "Everything is paused, so this was not approved. Resume first, then approve.",
        );
        control.refresh();
      } else if (err instanceof ApiError && err.status === 409) {
        setDecisionError(
          "This request changed while you were reviewing it. The latest version is shown below. Review it before deciding again.",
        );
        setRejecting(false);
        await reload();
      } else if (err instanceof ApiError && err.status === 401)
        router.replace("/auth/login");
      else setDecisionError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function saveEdit(edit: Edit) {
    if (!request || busy) return;
    setBusy(true);
    setDecisionError("");
    setNotice("");
    try {
      const saved = await api<Approval>(
        `/approvals/${encodeURIComponent(id)}/edit`,
        { method: "POST", body: JSON.stringify(edit) },
      );
      setEditing(false);
      setNotice(
        `Your changes are saved as version ${saved.version}. Nothing has been approved yet: review this version, then approve it.`,
      );
      await reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setDecisionError(
          "This request can no longer be edited. Its latest details are shown below.",
        );
        setEditing(false);
        await reload();
      } else if (err instanceof ApiError && err.status === 401)
        router.replace("/auth/login");
      else setDecisionError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  const mayDecide = user?.role === "owner" || user?.email === request?.approver;
  return (
    <>
      <Link className="back-link" href="/approvals">
        <ArrowLeft size={16} />
        Approval queue
      </Link>
      {loading && !request ? (
        <Loading label="Loading request details…" />
      ) : error && !request ? (
        <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>
      ) : (
        request && (
          <>
            <PageHeading
              eyebrow="REVIEW REQUEST"
              title={readable(request.tool)}
              action={<Badge status={request.status} />}
            >
              Requested by {readable(request.actor)} on{" "}
              {dateTime(request.created_at)}.
            </PageHeading>
            {error && (
              <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>
            )}
            {decisionError && <ErrorNotice>{decisionError}</ErrorNotice>}
            {notice && (
              <div className="notice notice-success" role="status">
                <Check size={18} />
                {notice}
              </div>
            )}
            <div className="detail-grid">
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>
                      <FileText size={18} />
                      {editing ? "Edit proposal" : "Exact inputs"}{" "}
                      <span className="version-pill">
                        Version {request.version}
                      </span>
                    </h2>
                    <p>
                      {editing
                        ? "Change the text below. Your changes are saved as a new version."
                        : "These are the values this operation will use."}
                    </p>
                  </div>
                  <button
                    className="icon-button"
                    aria-label="Refresh request"
                    onClick={() => void reload()}
                    disabled={busy || loading || editing}
                  >
                    <RefreshCw size={17} />
                  </button>
                </div>
                {editing && request.status === "pending" && mayDecide ? (
                  <EditForm
                    key={request.version}
                    request={request}
                    busy={busy}
                    onSave={(edit) => void saveEdit(edit)}
                    onCancel={() => setEditing(false)}
                  />
                ) : (
                  <InputFields
                    entries={Object.entries(request.inputs).filter(
                      ([key]) => key !== "idempotency_key",
                    )}
                  />
                )}
                {"idempotency_key" in request.inputs && (
                  <details className="technical-details">
                    <summary>Operation reference</summary>
                    <InputFields
                      entries={[
                        ["idempotency_key", request.inputs.idempotency_key],
                      ]}
                    />
                  </details>
                )}
                <div className="payload-note">
                  <ShieldCheck size={16} />
                  Request text is shown as data. Review all recipients, amounts
                  and content before deciding.
                </div>
              </section>
              <aside className="detail-sidebar">
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Request context</h2>
                  </div>
                  <dl className="meta-list">
                    <div>
                      <dt>Requested by</dt>
                      <dd>{readable(request.actor)}</dd>
                    </div>
                    <div>
                      <dt>Approver</dt>
                      <dd>{request.approver}</dd>
                    </div>
                    <div>
                      <dt>Tool</dt>
                      <dd>
                        <code>{request.tool}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Version</dt>
                      <dd>{request.version}</dd>
                    </div>
                    <div>
                      <dt>Reason</dt>
                      <dd className="preserve-text">
                        {request.reason || "No additional reason supplied."}
                      </dd>
                    </div>
                    {request.approved_by && (
                      <div>
                        <dt>Decided by</dt>
                        <dd>{request.approved_by}</dd>
                      </div>
                    )}
                  </dl>
                  {request.task_id && (
                    <div className="panel-bottom">
                      <DetailLink href={`/tasks/${request.task_id}`}>
                        View linked task
                      </DetailLink>
                    </div>
                  )}
                </section>
                <section className="decision-card">
                  <span className="decision-icon">
                    <ShieldCheck size={23} />
                  </span>
                  <h2>
                    {request.status === "pending"
                      ? "Your decision"
                      : "Decision recorded"}
                  </h2>
                  {request.status === "pending" ? (
                    <>
                      <p>
                        {editing
                          ? "You are editing this proposal. Save or cancel your changes to decide."
                          : "Approve this version to let the operation continue, edit it first, or reject it with a reason."}
                      </p>
                      {editing ? null : mayDecide ? (
                        rejecting ? (
                          <form
                            onSubmit={(e) => {
                              e.preventDefault();
                              void decide("reject");
                            }}
                          >
                            <label
                              className="field-label"
                              htmlFor="reject-reason"
                            >
                              Reason for rejection <span>(optional)</span>
                            </label>
                            <textarea
                              id="reject-reason"
                              value={reason}
                              onChange={(e) => setReason(e.target.value)}
                              rows={3}
                              placeholder="Let the requester know why…"
                              disabled={busy}
                            />
                            <button
                              className="button button-danger full-width"
                              type="submit"
                              disabled={busy}
                            >
                              {busy ? "Recording…" : "Confirm rejection"}
                            </button>
                            <button
                              className="button button-quiet full-width"
                              type="button"
                              disabled={busy}
                              onClick={() => setRejecting(false)}
                            >
                              Cancel
                            </button>
                          </form>
                        ) : (
                          <>
                            <button
                              className="button button-primary full-width is-blocked"
                              disabled={
                                busy || loading || Boolean(error) || paused
                              }
                              aria-describedby={
                                paused ? "approve-paused" : undefined
                              }
                              onClick={() => void decide("approve")}
                            >
                              <Check size={17} />
                              {busy
                                ? "Recording…"
                                : `Approve version ${request.version}`}
                            </button>
                            {paused && (
                              <p className="paused-note" id="approve-paused">
                                Everything is paused, so nothing can be
                                approved. Resume first. You can still edit or
                                reject this request.
                              </p>
                            )}
                            <button
                              className="button button-secondary full-width"
                              disabled={busy || loading || Boolean(error)}
                              onClick={() => {
                                setNotice("");
                                setDecisionError("");
                                setEditing(true);
                              }}
                            >
                              <Pencil size={15} />
                              Edit proposal
                            </button>
                            <button
                              className="button button-secondary full-width"
                              disabled={busy || loading || Boolean(error)}
                              onClick={() => setRejecting(true)}
                            >
                              <X size={16} />
                              Reject request
                            </button>
                          </>
                        )
                      ) : (
                        <p className="permission-note">
                          Only the owner or named approver can decide this
                          request.
                        </p>
                      )}
                    </>
                  ) : (
                    <p>
                      {request.status === "approved"
                        ? "Approved and waiting for the workflow to continue. Refresh to see its progress."
                        : request.status === "executed"
                          ? "This operation has been carried out. Open the linked task to see its activity."
                          : `This request is ${request.status}. Return to the queue for pending work.`}
                    </p>
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
