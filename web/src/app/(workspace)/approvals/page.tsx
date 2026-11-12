"use client";

import Link from "next/link";
import { ArrowRight, Clock3, Inbox, RefreshCw, Users } from "lucide-react";
import { useData } from "@/lib/use-data";
import { dateTime, readable, type Approval } from "@/lib/api";
import {
  Badge,
  Empty,
  ErrorNotice,
  Loading,
  PageHeading,
} from "@/components/ui";

export default function ApprovalsPage() {
  const { data, loading, error, reload } = useData<Approval[]>("/approvals");
  const count = data?.length ?? 0;
  const roles = new Set(data?.map((item) => item.actor)).size;
  const oldest = data?.length
    ? data.reduce((a, b) => (a.created_at < b.created_at ? a : b)).created_at
    : null;
  return (
    <>
      <PageHeading
        eyebrow="DECISIONS, WITH CONTEXT"
        title="Approval queue"
        action={
          <button
            className="button button-secondary"
            onClick={() => void reload()}
            disabled={loading}
          >
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      >
        A clear view of what needs you. Review each request before it moves
        forward.
      </PageHeading>
      <div className="stat-grid">
        <div className="stat-card">
          <span className="stat-icon teal">
            <Inbox size={19} />
          </span>
          <div>
            <p>Waiting for review</p>
            <strong>
              {data ? count : "—"}
              <small>{count === 1 ? "request" : "requests"}</small>
            </strong>
          </div>
        </div>
        <div className="stat-card">
          <span className="stat-icon blue">
            <Users size={19} />
          </span>
          <div>
            <p>Requesting roles</p>
            <strong>
              {data ? roles : "—"}
              <small>in this queue</small>
            </strong>
          </div>
        </div>
        <div className="stat-card stat-date">
          <span className="stat-icon amber">
            <Clock3 size={19} />
          </span>
          <div>
            <p>Oldest request</p>
            <strong>
              {oldest ? dateTime(oldest) : data ? "All clear" : "—"}
            </strong>
          </div>
        </div>
      </div>
      {error && <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              Needs your attention{" "}
              <span className="count-pill">{data ? count : "—"}</span>
            </h2>
            <p>Open a request to see exactly what you&apos;re approving.</p>
          </div>
          <span className="subtle-label">PENDING REQUESTS</span>
        </div>
        {loading && !data ? (
          <Loading label="Loading approval requests…" />
        ) : data?.length ? (
          <div
            className="table-wrap"
            role="region"
            aria-label="Approval requests"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  <th>Request</th>
                  <th>Requested by</th>
                  <th>Submitted</th>
                  <th>Status</th>
                  <th>
                    <span className="sr-only">Open request</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <Link
                        className="row-title"
                        href={`/approvals/${item.id}`}
                      >
                        {readable(item.tool)}
                      </Link>
                      <span className="row-subtitle">
                        {item.tool} <span>·</span> Version {item.version}
                      </span>
                    </td>
                    <td>
                      <div className="role-cell">
                        <span className="role-avatar">
                          {item.actor.slice(0, 1).toUpperCase()}
                        </span>
                        <span>{readable(item.actor)}</span>
                      </div>
                    </td>
                    <td className="date-cell">{dateTime(item.created_at)}</td>
                    <td>
                      <Badge status={item.status} />
                    </td>
                    <td>
                      <Link
                        href={`/approvals/${item.id}`}
                        className="table-action"
                        aria-label={`Review ${readable(item.tool)}`}
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
          !error && (
            <Empty title="You're all caught up">
              New requests will appear here when a role needs your decision.
            </Empty>
          )
        )}
      </section>
      <div className="quiet-note">
        <span className="live-dot" />
        <span>Approvals apply to the exact inputs and version you review.</span>
      </div>
    </>
  );
}
