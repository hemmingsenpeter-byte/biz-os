"use client";

import { useRef, useState } from "react";
import { OctagonPause, Pause, Play } from "lucide-react";
import {
  api,
  dateTime,
  errorMessage,
  person,
  type ControlState,
} from "@/lib/api";
import { ErrorNotice } from "./ui";

export function PauseControl({
  mayResume,
  onChange,
}: {
  mayResume: boolean;
  onChange: (state: ControlState) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function pause() {
    setBusy(true);
    setError("");
    try {
      const state = await api<ControlState>("/control/pause", {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() }),
      });
      setReason("");
      dialog.current?.close();
      onChange(state);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <button
        className="button button-secondary pause-button"
        onClick={() => {
          setError("");
          dialog.current?.showModal();
        }}
      >
        <Pause size={14} />
        Pause<span className="pause-button-rest"> everything</span>
      </button>
      <dialog
        ref={dialog}
        className="confirm-dialog"
        aria-labelledby="pause-title"
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void pause();
          }}
        >
          <span className="confirm-icon">
            <OctagonPause size={24} />
          </span>
          <h2 id="pause-title">Pause everything?</h2>
          <p>
            No role will start new work, nothing will be sent and nothing can be
            approved until {mayResume ? "you resume" : "the owner resumes"}.
            Incoming work is still collected, so nothing is lost.
          </p>
          {error && <ErrorNotice>{error}</ErrorNotice>}
          <label className="field-label" htmlFor="pause-reason">
            Reason <span>(optional)</span>
          </label>
          <input
            id="pause-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={300}
            placeholder="What made you stop things?"
            disabled={busy}
          />
          <div className="confirm-actions">
            <button
              className="button button-quiet"
              type="button"
              disabled={busy}
              onClick={() => dialog.current?.close()}
            >
              Cancel
            </button>
            <button
              className="button button-danger"
              type="submit"
              disabled={busy}
            >
              <Pause size={14} />
              {busy ? "Pausing…" : "Pause everything now"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}

export function PausedBanner({
  state,
  mayResume,
  onChange,
}: {
  state: ControlState;
  mayResume: boolean;
  onChange: (state: ControlState) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function resume() {
    setBusy(true);
    setError("");
    try {
      onChange(await api<ControlState>("/control/resume", { method: "POST" }));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <section className="paused-banner" role="alert" aria-label="Paused">
      <OctagonPause size={26} />
      <div>
        <p className="paused-title">Everything is paused</p>
        <p>
          Paused by {person(state.by)}
          {state.at ? ` on ${dateTime(state.at)}` : ""}.{" "}
          {state.reason ? (
            <>
              Reason: <span className="preserve-text">{state.reason}</span>
            </>
          ) : (
            "No reason was given."
          )}
        </p>
        <p>
          No role starts new work, nothing is sent and nothing can be approved
          until this is resumed. Incoming work is still collected.
        </p>
        {error && <p className="paused-error">{error}</p>}
      </div>
      {mayResume ? (
        <button
          className="button paused-resume"
          disabled={busy}
          onClick={() => void resume()}
        >
          <Play size={14} />
          {busy ? "Resuming…" : "Resume"}
        </button>
      ) : (
        <span className="paused-owner-only">Only the owner can resume.</span>
      )}
    </section>
  );
}
