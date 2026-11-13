"use client";

import Link from "next/link";
import {
  ArrowDown,
  CheckCheck,
  CircleAlert,
  Clock3,
  Coins,
  Receipt,
  RefreshCw,
} from "lucide-react";
import type { ReactNode } from "react";
import {
  dateTime,
  person,
  readable,
  SEVERITIES,
  type Correction,
  type Metrics,
} from "@/lib/api";
import { useData } from "@/lib/use-data";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";

const DAYS = 28;
const NOT_MEASURED = "Not measured yet";

function day(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}
function money(value: number) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
  }).format(value);
}
function duration(minutes: number) {
  const total = Math.round(Math.abs(minutes));
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  const text =
    hours && rest
      ? `${hours} h ${rest} min`
      : hours
        ? `${hours} h`
        : `${rest} min`;
  return minutes < 0 ? `−${text}` : text;
}
// A missing measurement is said in words; it is never shown as a number.
function Measured({
  value,
  format,
}: {
  value: number | null;
  format: (value: number) => string;
}) {
  return value === null ? (
    <span className="not-measured">{NOT_MEASURED}</span>
  ) : (
    <>{format(value)}</>
  );
}
const percent = (rate: number) => `${Math.round(rate * 100)}%`;

function Stat({
  icon,
  tone,
  label,
  children,
  note,
}: {
  icon: ReactNode;
  tone: string;
  label: string;
  children: ReactNode;
  note?: string;
}) {
  return (
    <div className="stat-card">
      <span className={`stat-icon ${tone}`}>{icon}</span>
      <div>
        <p>{label}</p>
        <strong>{children}</strong>
        {note && <span className="stat-note">{note}</span>}
      </div>
    </div>
  );
}

function text(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function CorrectionItem({ item }: { item: Correction }) {
  const changed = [
    ...new Set([...Object.keys(item.before), ...Object.keys(item.after)]),
  ].filter(
    (key) =>
      JSON.stringify(item.before[key]) !== JSON.stringify(item.after[key]),
  );
  const severity = SEVERITIES.find((option) => option.value === item.severity);
  return (
    <li className="correction">
      <div className="correction-heading">
        <div>
          <Link className="row-title" href={`/approvals/${item.op_id}`}>
            {readable(item.actor)} <span>·</span> {readable(item.tool)}
          </Link>
          <span className="row-subtitle">
            Version {item.from_version} to {item.to_version} <span>·</span>{" "}
            edited by {person(item.by)} on {dateTime(item.at)}
          </span>
        </div>
        <span className={`badge severity-${item.severity ?? "none"}`}>
          <span className="badge-dot" />
          {severity?.label ?? "Kind of change not given"}
        </span>
      </div>
      <p className="correction-reason">
        <span>Reason</span>
        {item.reason || "No reason was given."}
      </p>
      {changed.map((key) => (
        <div key={key} className="correction-change">
          <p className="correction-field">{readable(key)}</p>
          <div className="correction-text">
            <span>Draft</span>
            <pre>{text(item.before[key])}</pre>
          </div>
          <ArrowDown size={14} aria-hidden="true" />
          <div className="correction-text is-edit">
            <span>Edit</span>
            <pre>{text(item.after[key])}</pre>
          </div>
        </div>
      ))}
    </li>
  );
}

export default function ScorecardPage() {
  const metrics = useData<Metrics>(`/metrics?days=${DAYS}`);
  const corrections = useData<Correction[]>("/corrections?limit=20");
  const data = metrics.data;
  const loading = metrics.loading || corrections.loading;
  const reload = () => {
    void metrics.reload();
    void corrections.reload();
  };
  return (
    <>
      <PageHeading
        eyebrow="RESULTS, MEASURED"
        title="Scorecard"
        action={
          <button
            className="button button-secondary"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      >
        How each role&apos;s proposals held up over the last {DAYS} days
        {data ? `, ${day(data.since)} to ${day(data.until)}` : ""}. A role earns
        more independence from this record.
      </PageHeading>
      {metrics.error && (
        <ErrorNotice retry={() => void metrics.reload()}>
          {metrics.error}
        </ErrorNotice>
      )}
      <div className="stat-grid stat-grid-five">
        <Stat
          icon={<CheckCheck size={19} />}
          tone="teal"
          label="Tasks completed"
        >
          {data ? data.tasks_completed : "—"}
        </Stat>
        <Stat
          icon={<CircleAlert size={19} />}
          tone={data?.serious_errors ? "red" : "teal"}
          label="Serious errors"
          note="Actions that had to be undone"
        >
          {data ? data.serious_errors : "—"}
        </Stat>
        <Stat icon={<Coins size={19} />} tone="blue" label="Cost">
          {data ? money(data.cost_usd) : "—"}
        </Stat>
        <Stat icon={<Receipt size={19} />} tone="blue" label="Cost per task">
          {data ? (
            <Measured value={data.cost_per_task_usd} format={money} />
          ) : (
            "—"
          )}
        </Stat>
        <Stat
          icon={<Clock3 size={19} />}
          tone="amber"
          label="Time saved"
          note={
            data && data.time_saved_minutes === null
              ? "Needs your timed baseline per task"
              : "After your review time"
          }
        >
          {data ? (
            <Measured value={data.time_saved_minutes} format={duration} />
          ) : (
            "—"
          )}
        </Stat>
      </div>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              By role and type of decision{" "}
              <span className="count-pill">
                {data ? data.decision_types.length : "—"}
              </span>
            </h2>
            <p>
              What you did with each proposal. Rates count approved proposals
              only.
            </p>
          </div>
          <span className="subtle-label">LAST {DAYS} DAYS</span>
        </div>
        {metrics.loading && !data ? (
          <Loading label="Loading the scorecard…" />
        ) : data?.decision_types.length ? (
          <div
            className="table-wrap score-table"
            role="region"
            aria-label="Results by role and type of decision"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  <th>Role and decision</th>
                  <th>Proposed</th>
                  <th>Approved as is</th>
                  <th>Wording-only edits</th>
                  <th>Approved with errors</th>
                  <th>Rejected</th>
                  <th>Waiting</th>
                  <th>Approved as is rate</th>
                  <th>Error-free rate</th>
                </tr>
              </thead>
              <tbody>
                {data.decision_types.map((row) => (
                  <tr key={`${row.role}/${row.decision_type}`}>
                    <td>
                      <span className="row-title">{readable(row.role)}</span>
                      <span className="row-subtitle">
                        {row.decision_type
                          ? readable(row.decision_type)
                          : "No decision type"}
                      </span>
                    </td>
                    <td data-label="Proposed">{row.proposals}</td>
                    <td data-label="Approved as is">
                      {row.approved_unchanged}
                    </td>
                    <td data-label="Wording-only edits">
                      {row.approved_cosmetic}
                    </td>
                    <td data-label="Approved with errors">
                      {row.approved_with_errors}
                    </td>
                    <td data-label="Rejected">{row.rejected}</td>
                    <td data-label="Waiting">{row.pending}</td>
                    <td data-label="Approved as is rate">
                      <Measured
                        value={row.approval_without_edit_rate}
                        format={percent}
                      />
                    </td>
                    <td data-label="Error-free rate">
                      <Measured value={row.error_free_rate} format={percent} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !metrics.error && (
            <Empty title="Nothing to score yet">
              Results appear here once a role has made a proposal in this
              period.
            </Empty>
          )
        )}
      </section>
      <div className="quiet-note">
        <span className="live-dot" />
        <span>
          Approved with errors means you corrected a fact or a broken rule
          before approving. Error-free counts approvals that needed no edit or
          wording changes only.
        </span>
      </div>
      {corrections.error && (
        <ErrorNotice retry={() => void corrections.reload()}>
          {corrections.error}
        </ErrorNotice>
      )}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              Recent corrections{" "}
              <span className="count-pill">
                {corrections.data ? corrections.data.length : "—"}
              </span>
            </h2>
            <p>
              The draft, your edit and your reason. These are what the roles
              learn from.
            </p>
          </div>
          <span className="subtle-label">NEWEST FIRST</span>
        </div>
        {corrections.loading && !corrections.data ? (
          <Loading label="Loading corrections…" />
        ) : corrections.data?.length ? (
          <ul className="correction-list">
            {corrections.data.map((item) => (
              <CorrectionItem key={item.id} item={item} />
            ))}
          </ul>
        ) : (
          !corrections.error && (
            <Empty title="No corrections yet">
              When you edit a proposal before approving it, the change and your
              reason are listed here.
            </Empty>
          )
        )}
      </section>
    </>
  );
}
