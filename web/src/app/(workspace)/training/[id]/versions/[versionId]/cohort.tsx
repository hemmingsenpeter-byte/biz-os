"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, errorMessage } from "@/lib/api";
import type {
  TrainingCandidate,
  TrainingCohort,
  TrainingVersion,
} from "@/lib/training";
import { ErrorNotice, Loading } from "@/components/ui";

export default function CohortForm({ version }: { version: TrainingVersion }) {
  const [options, setOptions] = useState<TrainingVersion[]>([version]);
  const [selected, setSelected] = useState<string[]>([version.id]);
  const [name, setName] = useState(
    `Version ${version.number} tuning comparison`,
  );
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [cohort, setCohort] = useState<TrainingCohort | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const roots = await api<TrainingCandidate[]>("/training/candidates", {
          signal: controller.signal,
        });
        const histories = await Promise.all(
          roots.map((root) =>
            api<TrainingVersion[]>(`/training/candidates/${root.id}/versions`, {
              signal: controller.signal,
            }),
          ),
        );
        if (!controller.signal.aborted) {
          const accepted = histories
            .flat()
            .filter(
              (item) =>
                item.status === "accepted_for_testing" &&
                item.task === version.task &&
                (item.candidate_id !== version.candidate_id ||
                  item.id === version.id),
            );
          setOptions([
            version,
            ...accepted.filter((item) => item.id !== version.id),
          ]);
        }
      } catch (failure) {
        if (!controller.signal.aborted) setProblem(errorMessage(failure));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [version.id, version.candidate_id, version.task, version]);
  async function freeze() {
    if (busy || !name.trim()) return;
    setBusy(true);
    setProblem("");
    try {
      setCohort(
        await api<TrainingCohort>("/training/cohorts", {
          method: "POST",
          body: JSON.stringify({ name: name.trim(), version_ids: selected }),
        }),
      );
    } catch (failure) {
      setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel training-form">
      <div className="panel-heading">
        <h2>Freeze a private tuning cohort</h2>
      </div>
      <div className="training-content">
        <p>
          Select the reviewed target and older cases to compare. Each correction
          can contribute one case. The server checks matching task, schema and
          source provenance.
        </p>
        {loading ? (
          <Loading label="Loading reviewed versions…" />
        ) : (
          <>
            <label className="field-label" htmlFor="cohort-name">
              Cohort name
            </label>
            <input
              id="cohort-name"
              value={name}
              maxLength={200}
              disabled={busy || Boolean(cohort)}
              onChange={(event) => setName(event.target.value)}
            />
            <fieldset disabled={busy || Boolean(cohort)}>
              <legend>Reviewed tuning cases</legend>
              {options.map((option) => (
                <label className="field-label" key={option.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(option.id)}
                    disabled={option.id === version.id}
                    onChange={(event) => {
                      setSelected((current) =>
                        event.target.checked
                          ? [
                              ...current.filter(
                                (id) =>
                                  options.find((item) => item.id === id)
                                    ?.candidate_id !== option.candidate_id,
                              ),
                              option.id,
                            ]
                          : current.filter((id) => id !== option.id),
                      );
                    }}
                  />
                  {option.id === version.id ? "Target case" : "Older case"} ·
                  Version {option.number}: {option.rule}
                </label>
              ))}
            </fieldset>
            {cohort ? (
              <Link
                className="button button-primary"
                href={`/training/cohorts/${cohort.id}?version=${version.id}`}
              >
                Review frozen cohort
              </Link>
            ) : (
              <button
                className="button button-primary"
                disabled={busy || !name.trim() || !selected.length}
                onClick={() => void freeze()}
              >
                {busy ? "Freezing cohort…" : "Freeze selected tuning cases"}
              </button>
            )}
          </>
        )}
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
      </div>
    </section>
  );
}
