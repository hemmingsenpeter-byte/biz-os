"use client";

import Link from "next/link";
import { use } from "react";
import { ArrowLeft, CheckCheck, Clock3, RefreshCw } from "lucide-react";
import {
  dateTime,
  readable,
  type Activity,
  type Approval,
  type Task,
} from "@/lib/api";
import { useData } from "@/lib/use-data";
import {
  Badge,
  DetailLink,
  Empty,
  ErrorNotice,
  Loading,
  PageHeading,
} from "@/components/ui";

export default function TaskPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const task = useData<Task>(`/tasks/${encodeURIComponent(id)}`);
  const activity = useData<Activity[]>(
    `/tasks/${encodeURIComponent(id)}/activity`,
  );
  const approvals = useData<Approval[]>("/approvals");
  const linked = approvals.data?.filter((item) => item.task_id === id) ?? [];
  return (
    <>
      <Link className="back-link" href="/tasks">
        <ArrowLeft size={16} />
        Tasks
      </Link>
      {task.loading && !task.data ? (
        <Loading label="Loading task…" />
      ) : task.error && !task.data ? (
        <ErrorNotice retry={() => void task.reload()}>{task.error}</ErrorNotice>
      ) : (
        task.data && (
          <>
            <PageHeading
              eyebrow="TASK DETAILS"
              title={task.data.title}
              action={<Badge status={task.data.status} />}
            >
              Track the work and the evidence behind its status.
            </PageHeading>
            {task.error && (
              <ErrorNotice retry={() => void task.reload()}>
                {task.error}
              </ErrorNotice>
            )}
            <div className="detail-grid">
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>
                      <Clock3 size={18} />
                      Activity
                    </h2>
                    <p>
                      Recorded events for this task and its shared job context.
                    </p>
                  </div>
                  <button
                    className="icon-button"
                    aria-label="Refresh activity"
                    onClick={() => {
                      void task.reload();
                      void activity.reload();
                      void approvals.reload();
                    }}
                  >
                    <RefreshCw size={17} />
                  </button>
                </div>
                {activity.error ? (
                  <div className="panel-padding">
                    <ErrorNotice retry={() => void activity.reload()}>
                      {activity.error}
                    </ErrorNotice>
                  </div>
                ) : activity.loading && !activity.data ? (
                  <Loading label="Loading activity…" />
                ) : activity.data?.length ? (
                  <ol className="timeline">
                    {activity.data.map((event) => (
                      <li key={event.id}>
                        <span className="timeline-icon">
                          <CheckCheck size={16} />
                        </span>
                        <div>
                          <div className="timeline-title">
                            <h3>{readable(event.kind)}</h3>
                            <time dateTime={event.at}>
                              {dateTime(event.at)}
                            </time>
                          </div>
                          {event.ref && (
                            <p className="event-ref">{event.ref}</p>
                          )}
                          {!event.task_id && (
                            <span className="event-scope">
                              Shared job evidence
                            </span>
                          )}
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <Empty title="No recorded activity yet">
                    Events will appear here as the workflow progresses.
                  </Empty>
                )}
              </section>
              <aside className="detail-sidebar">
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Task context</h2>
                  </div>
                  <dl className="meta-list">
                    <div>
                      <dt>Status</dt>
                      <dd>
                        <Badge status={task.data.status} />
                      </dd>
                    </div>
                    <div>
                      <dt>Kind</dt>
                      <dd>{readable(task.data.kind)}</dd>
                    </div>
                    <div>
                      <dt>Assigned to</dt>
                      <dd>
                        {task.data.assignee
                          ? readable(task.data.assignee)
                          : "Unassigned"}
                      </dd>
                    </div>
                    <div>
                      <dt>Due</dt>
                      <dd>{dateTime(task.data.due_at)}</dd>
                    </div>
                    <div>
                      <dt>Goal</dt>
                      <dd>
                        {task.data.goal_key
                          ? readable(task.data.goal_key)
                          : "Not linked"}
                      </dd>
                    </div>
                    <div>
                      <dt>Task ID</dt>
                      <dd>
                        <code>{task.data.id}</code>
                      </dd>
                    </div>
                  </dl>
                </section>
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Pending approvals</h2>
                  </div>
                  {approvals.error ? (
                    <div className="panel-padding">
                      <ErrorNotice retry={() => void approvals.reload()}>
                        {approvals.error}
                      </ErrorNotice>
                    </div>
                  ) : approvals.loading && !approvals.data ? (
                    <Loading label="Loading approvals…" />
                  ) : linked.length ? (
                    <div className="linked-items">
                      {linked.map((item) => (
                        <DetailLink
                          key={item.id}
                          href={`/approvals/${item.id}`}
                        >
                          {readable(item.tool)}
                        </DetailLink>
                      ))}
                    </div>
                  ) : (
                    <p className="panel-small-copy">
                      No pending approvals linked to this task.
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
