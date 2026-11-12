"use client";

import Link from "next/link";
import { ArrowRight, RefreshCw } from "lucide-react";
import { useState } from "react";
import { dateTime, readable, type Task } from "@/lib/api";
import { useData } from "@/lib/use-data";
import {
  Badge,
  Empty,
  ErrorNotice,
  Loading,
  PageHeading,
} from "@/components/ui";

export default function TasksPage() {
  const { data, error, loading, reload } = useData<Task[]>("/tasks");
  const [filter, setFilter] = useState("all");
  const statuses = ["all", ...new Set(data?.map((task) => task.status) ?? [])];
  const tasks = data?.filter(
    (task) => filter === "all" || task.status === filter,
  );
  return (
    <>
      <PageHeading
        eyebrow="WORK, IN VIEW"
        title="Tasks"
        action={
          <button
            className="button button-secondary"
            disabled={loading}
            onClick={() => void reload()}
          >
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      >
        Follow work from its first step to a completed, evidenced result.
      </PageHeading>
      {error && <ErrorNotice retry={() => void reload()}>{error}</ErrorNotice>}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              All work <span className="count-pill">{data?.length ?? "—"}</span>
            </h2>
            <p>Open a task for its context and recorded activity.</p>
          </div>
          <label className="filter-label">
            Status
            <select
              aria-label="Filter by status"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            >
              {statuses.map((status) => (
                <option key={status} value={status}>
                  {status === "all" ? "All statuses" : readable(status)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {loading && !data ? (
          <Loading label="Loading tasks…" />
        ) : tasks?.length ? (
          <div
            className="table-wrap"
            role="region"
            aria-label="Task list"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Assigned to</th>
                  <th>Due</th>
                  <th>Status</th>
                  <th>
                    <span className="sr-only">View task</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <Link className="row-title" href={`/tasks/${task.id}`}>
                        {task.title}
                      </Link>
                      <span className="row-subtitle">
                        {readable(task.kind)}
                      </span>
                    </td>
                    <td>
                      {task.assignee ? (
                        readable(task.assignee)
                      ) : (
                        <span className="muted">Unassigned</span>
                      )}
                    </td>
                    <td className="date-cell">
                      {task.due_at ? (
                        dateTime(task.due_at)
                      ) : (
                        <span className="muted">No due date</span>
                      )}
                    </td>
                    <td>
                      <Badge status={task.status} />
                    </td>
                    <td>
                      <Link
                        className="table-action"
                        href={`/tasks/${task.id}`}
                        aria-label={`View ${task.title}`}
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
            <Empty
              title={filter === "all" ? "No tasks yet" : "No matching tasks"}
            >
              {filter === "all"
                ? "Tasks will appear here as your roles start work."
                : "Choose another status to see more work."}
            </Empty>
          )
        )}
      </section>
    </>
  );
}
