"use client";

import Link from "next/link";
import { use, useState } from "react";
import { dateTime } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type { TrainingCandidate, TrainingVersion } from "@/lib/training";
import {
  TrainingAccess,
  TrainingBoundary,
  VersionStatus,
} from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";
import VersionEditor from "./editor";

function VersionHistory({
  id,
  candidate,
  ordered,
  refresh,
}: {
  id: string;
  candidate: TrainingCandidate;
  ordered: TrainingVersion[];
  refresh: () => Promise<unknown>;
}) {
  // Keep an in-progress first editor mounted when a concurrent initial save adds history.
  const [creatingFirst] = useState(ordered.length === 0);
  return (
    <div className="training-stack">
      {ordered.length ? (
        <section className="panel">
          <div className="panel-heading">
            <h2>Immutable versions</h2>
          </div>
          <div className="training-content">
            <ol>
              {ordered.map((version) => (
                <li key={version.id}>
                  <Link
                    className="row-title"
                    href={`/training/${id}/versions/${version.id}`}
                  >
                    Version {version.number}
                  </Link>
                  <p>
                    {dateTime(version.at)} · <VersionStatus version={version} />
                  </p>
                  <p className="preserve-text">{version.rule}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>
      ) : (
        <Empty title="No candidate versions yet">
          Create a version, then review its exact instructions for testing.
        </Empty>
      )}
      {creatingFirst && (
        <VersionEditor
          candidate={candidate}
          source={null}
          latest={ordered[ordered.length - 1] ?? null}
          refresh={refresh}
        />
      )}
      {ordered.length > 0 && (
        <Link
          className="button button-secondary"
          href={`/training/${id}/versions/${ordered[ordered.length - 1].id}`}
        >
          Open latest version
        </Link>
      )}
    </div>
  );
}

function OwnerVersions({ id }: { id: string }) {
  const candidate = useData<TrainingCandidate>(
    `/training/candidates/${encodeURIComponent(id)}`,
  );
  const versions = useData<TrainingVersion[]>(
    `/training/candidates/${encodeURIComponent(id)}/versions`,
  );
  const ordered = [...(versions.data ?? [])].sort(
    (a, b) => a.number - b.number,
  );
  return (
    <>
      <Link className="back-link" href={`/training/${id}`}>
        Original proposal
      </Link>
      <PageHeading eyebrow="PRIVATE TRAINING" title="Candidate version history">
        Every version keeps its own content, source and owner testing review.
      </PageHeading>
      <TrainingBoundary />
      {(candidate.error || versions.error) && (
        <ErrorNotice
          retry={() => {
            void candidate.reload();
            void versions.reload();
          }}
        >
          {candidate.error || versions.error}
        </ErrorNotice>
      )}
      {(candidate.loading && !candidate.data) ||
      (versions.loading && !versions.data) ? (
        <Loading label="Loading version history…" />
      ) : (
        candidate.data &&
        versions.data && (
          <VersionHistory
            id={id}
            candidate={candidate.data}
            ordered={ordered}
            refresh={versions.reload}
          />
        )
      )}
    </>
  );
}

export default function VersionsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <TrainingAccess>
      <OwnerVersions id={id} />
    </TrainingAccess>
  );
}
