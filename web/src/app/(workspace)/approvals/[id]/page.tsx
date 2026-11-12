"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Check,
  FileText,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  dateTime,
  errorMessage,
  readable,
  type Approval,
} from "@/lib/api";
import { useData } from "@/lib/use-data";
import {
  Badge,
  DetailLink,
  ErrorNotice,
  Loading,
  PageHeading,
} from "@/components/ui";
import { useUser } from "@/components/workspace";

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
      if (err instanceof ApiError && err.status === 409) {
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
                      Exact inputs{" "}
                      <span className="version-pill">
                        Version {request.version}
                      </span>
                    </h2>
                    <p>These are the values this operation will use.</p>
                  </div>
                  <button
                    className="icon-button"
                    aria-label="Refresh request"
                    onClick={() => void reload()}
                    disabled={busy || loading}
                  >
                    <RefreshCw size={17} />
                  </button>
                </div>
                <InputFields
                  entries={Object.entries(request.inputs).filter(
                    ([key]) => key !== "idempotency_key",
                  )}
                />
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
                        Approve this version to let the operation continue, or
                        reject it with a reason.
                      </p>
                      {mayDecide ? (
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
                              className="button button-primary full-width"
                              disabled={busy || loading || Boolean(error)}
                              onClick={() => void decide("approve")}
                            >
                              <Check size={17} />
                              {busy
                                ? "Recording…"
                                : `Approve version ${request.version}`}
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
