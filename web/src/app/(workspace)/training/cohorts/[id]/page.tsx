"use client";

import Link from "next/link";
import { use, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, errorMessage, isPausedError } from "@/lib/api";
import { useData } from "@/lib/use-data";
import type {
  ComparisonModel,
  TrainingCohort,
  TrainingComparison,
  TrainingVersion,
} from "@/lib/training";
import {
  FrozenCohort,
  TrainingAccess,
  TrainingBoundary,
  TrainingValue,
} from "@/components/training";
import { Empty, ErrorNotice, Loading, PageHeading } from "@/components/ui";
import { useControl } from "@/components/workspace";

function ComparisonForm({
  cohort,
  versionId,
}: {
  cohort: TrainingCohort;
  versionId: string;
}) {
  const version = useData<TrainingVersion>(
    `/training/versions/${encodeURIComponent(versionId)}`,
  );
  const router = useRouter();
  const control = useControl();
  const [name, setName] = useState("");
  const [digest, setDigest] = useState("");
  const [ollamaVersion, setOllamaVersion] = useState("");
  const [backendVersion, setBackendVersion] = useState("");
  const [temperature, setTemperature] = useState("0.1");
  const [seed, setSeed] = useState("0");
  const [numPredict, setNumPredict] = useState("2048");
  const [timeout, setTimeoutSeconds] = useState("90");
  const [repetitions, setRepetitions] = useState("1");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const request = useRef<{ contract: string; key: string } | null>(null);
  async function enqueue() {
    if (
      busy ||
      version.data?.status !== "accepted_for_testing" ||
      cohort.status !== "accepted_for_testing"
    )
      return;
    const model: ComparisonModel = {
      name: name.trim(),
      digest: digest.trim(),
      ollama_version: ollamaVersion.trim(),
      backend_version: backendVersion.trim(),
      base_url: "http://127.0.0.1:11434",
      temperature: Number(temperature),
      seed: Number(seed),
      num_predict: Number(numPredict),
      timeout_seconds: Number(timeout),
      repetitions: Number(repetitions),
    };
    if (
      !model.name ||
      !model.digest ||
      !model.ollama_version ||
      !model.backend_version ||
      !Object.values(model)
        .filter((value) => typeof value === "number")
        .every(Number.isFinite) ||
      model.temperature < 0 ||
      model.temperature > 2 ||
      !Number.isInteger(model.seed) ||
      model.seed < -2147483648 ||
      model.seed > 2147483647 ||
      !Number.isInteger(model.num_predict) ||
      model.num_predict < 1 ||
      model.num_predict > 8192 ||
      model.timeout_seconds <= 0 ||
      model.timeout_seconds > 600 ||
      !Number.isInteger(model.repetitions) ||
      model.repetitions < 1 ||
      model.repetitions > 10
    ) {
      setProblem(
        "Enter the installed model name, digest and software versions, with valid generation settings.",
      );
      return;
    }
    const payload = { version_id: versionId, cohort_id: cohort.id, model };
    const contract = JSON.stringify(payload);
    if (request.current?.contract !== contract)
      request.current = { contract, key: crypto.randomUUID() };
    setBusy(true);
    setProblem("");
    try {
      const job = await api<TrainingComparison>("/training/comparisons", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          idempotency_key: request.current.key,
        }),
      });
      router.push(`/training/comparisons/${job.id}`);
    } catch (failure) {
      if (isPausedError(failure)) {
        control.refresh();
        setProblem(
          "Everything is paused. Resume before queueing a comparison.",
        );
      } else setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  if (version.error)
    return (
      <ErrorNotice retry={() => void version.reload()}>
        {version.error}
      </ErrorNotice>
    );
  if (!version.data)
    return <Loading label="Checking version-specific review…" />;
  if (
    version.data.status !== "accepted_for_testing" ||
    !version.data.eligible_for_testing
  )
    return (
      <Empty title="This version cannot run">
        The exact version needs complete source provenance and its own owner
        acceptance for testing.
      </Empty>
    );
  return (
    <section className="panel training-form">
      <div className="panel-heading">
        <h2>Queue a paired local comparison</h2>
      </div>
      <div className="training-content">
        <h3>Target version {version.data.number}</h3>
        <TrainingValue value={version.data.rule} />
        <p>
          Fresh baseline and candidate inference use the same pinned local model
          and frozen cases. The host-side worker verifies the installed digest
          and settings before inference.
        </p>
        <p className="field-hint">
          The comparison runs in the background on your local machine. Its
          comparison worker must be running; you can close this page after
          queueing.
        </p>
        <label className="field-label" htmlFor="comparison-model">
          Installed model name
        </label>
        <input
          id="comparison-model"
          value={name}
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
          placeholder="Exact installed Ollama model name"
        />
        <label className="field-label" htmlFor="comparison-digest">
          Installed model digest
        </label>
        <input
          id="comparison-digest"
          value={digest}
          disabled={busy}
          onChange={(event) => setDigest(event.target.value)}
          placeholder="Verified installed digest"
        />
        <label className="field-label" htmlFor="comparison-ollama">
          Ollama version
        </label>
        <input
          id="comparison-ollama"
          value={ollamaVersion}
          disabled={busy}
          onChange={(event) => setOllamaVersion(event.target.value)}
        />
        <label className="field-label" htmlFor="comparison-backend">
          Model adapter version
        </label>
        <input
          id="comparison-backend"
          value={backendVersion}
          disabled={busy}
          onChange={(event) => setBackendVersion(event.target.value)}
        />
        <details className="training-source">
          <summary>Frozen prompts and generation settings</summary>
          <h3>Baseline instructions</h3>
          <TrainingValue value={version.data.baseline_system} />
          <h3>Candidate instructions</h3>
          <TrainingValue value={version.data.system} />
          <p>Endpoint: http://127.0.0.1:11434</p>
          <label className="field-label" htmlFor="comparison-temperature">
            Temperature
          </label>
          <input
            id="comparison-temperature"
            type="number"
            step="0.1"
            value={temperature}
            disabled={busy}
            onChange={(event) => setTemperature(event.target.value)}
          />
          <label className="field-label" htmlFor="comparison-seed">
            Seed
          </label>
          <input
            id="comparison-seed"
            type="number"
            step="1"
            value={seed}
            disabled={busy}
            onChange={(event) => setSeed(event.target.value)}
          />
          <label className="field-label" htmlFor="comparison-predict">
            Maximum generated tokens
          </label>
          <input
            id="comparison-predict"
            type="number"
            step="1"
            value={numPredict}
            disabled={busy}
            onChange={(event) => setNumPredict(event.target.value)}
          />
          <label className="field-label" htmlFor="comparison-timeout">
            Timeout per attempt (seconds)
          </label>
          <input
            id="comparison-timeout"
            type="number"
            value={timeout}
            disabled={busy}
            onChange={(event) => setTimeoutSeconds(event.target.value)}
          />
          <label className="field-label" htmlFor="comparison-repetitions">
            Repetitions
          </label>
          <input
            id="comparison-repetitions"
            type="number"
            step="1"
            value={repetitions}
            disabled={busy}
            onChange={(event) => setRepetitions(event.target.value)}
          />
        </details>
        {problem && <ErrorNotice>{problem}</ErrorNotice>}
        <button
          className="button button-primary"
          disabled={busy || Boolean(control.state?.paused)}
          onClick={() => void enqueue()}
        >
          {busy ? "Queueing…" : "Queue local comparison"}
        </button>
        <p className="field-hint">
          No prompt, rule, policy or autonomy is activated by this comparison.
        </p>
      </div>
    </section>
  );
}

function OwnerCohort({ id }: { id: string }) {
  const cohort = useData<TrainingCohort>(
    `/training/cohorts/${encodeURIComponent(id)}`,
  );
  const params = useSearchParams();
  const [selectedVersion, setSelectedVersion] = useState(
    params.get("version") || "",
  );
  const control = useControl();
  const [reviewed, setReviewed] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const members = cohort.data?.members ?? [];
  const targetVersion = members.some(
    (member) => member.version_id === selectedVersion,
  )
    ? selectedVersion
    : members[0]?.version_id || "";
  async function attest() {
    if (!reviewed || busy) return;
    setBusy(true);
    setProblem("");
    try {
      await api(`/training/cohorts/${id}/attest`, {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() }),
      });
      await cohort.reload();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        setProblem(
          "An attestation is already recorded. Your note is kept; inspect the frozen record.",
        );
        await cohort.reload();
      } else setProblem(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Link className="back-link" href="/training">
        Training
      </Link>
      <PageHeading
        eyebrow="PRIVATE TUNING DATA"
        title="Review frozen comparison cohort"
      >
        Attest the exact case membership and expected checks before queueing
        work.
      </PageHeading>
      <TrainingBoundary />
      {cohort.error ? (
        <ErrorNotice retry={() => void cohort.reload()}>
          {cohort.error}
        </ErrorNotice>
      ) : !cohort.data ? (
        <Loading label="Loading frozen cohort…" />
      ) : (
        <div className="training-stack">
          <FrozenCohort cohort={cohort.data} />
          {cohort.data.status === "proposed" ? (
            <section className="panel training-form">
              <div className="panel-heading">
                <h2>Owner tuning attestation</h2>
              </div>
              <div className="training-content">
                <label className="field-label">
                  <input
                    type="checkbox"
                    checked={reviewed}
                    disabled={busy}
                    onChange={(event) => setReviewed(event.target.checked)}
                  />{" "}
                  I reviewed this frozen tuning cohort and its expected results.
                </label>
                <label className="field-label" htmlFor="cohort-reason">
                  Attestation note
                </label>
                <textarea
                  id="cohort-reason"
                  rows={3}
                  maxLength={2000}
                  value={reason}
                  disabled={busy}
                  onChange={(event) => setReason(event.target.value)}
                />
                <button
                  className="button button-primary"
                  disabled={busy || !reviewed || Boolean(control.state?.paused)}
                  onClick={() => void attest()}
                >
                  Attest this frozen cohort for testing
                </button>
              </div>
            </section>
          ) : (
            <>
              <section className="panel training-form">
                <div className="training-content">
                  <label className="field-label" htmlFor="comparison-target">
                    Target candidate version
                  </label>
                  <select
                    id="comparison-target"
                    value={targetVersion}
                    onChange={(event) => setSelectedVersion(event.target.value)}
                  >
                    {members.map((member) => (
                      <option key={member.version_id} value={member.version_id}>
                        Case {member.case_id} · Version {member.version_id}
                      </option>
                    ))}
                  </select>
                </div>
              </section>
              <ComparisonForm
                key={targetVersion}
                cohort={cohort.data}
                versionId={targetVersion}
              />
            </>
          )}
          {problem && <ErrorNotice>{problem}</ErrorNotice>}
        </div>
      )}
    </>
  );
}

export default function CohortPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <TrainingAccess>
      <OwnerCohort key={id} id={id} />
    </TrainingAccess>
  );
}
