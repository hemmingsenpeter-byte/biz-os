"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import {
  ArrowRight,
  Check,
  Fingerprint,
  KeyRound,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
} from "@simplewebauthn/browser";
import { api, ApiError, errorMessage, type User } from "@/lib/api";
import { ErrorNotice, Mark } from "./ui";

type Registration = {
  challenge_id: string;
  options: Parameters<typeof startRegistration>[0]["optionsJSON"];
};
type Authentication = {
  challenge_id: string;
  options: Parameters<typeof startAuthentication>[0]["optionsJSON"];
};

function friendlyAuthError(error: unknown) {
  const name = error instanceof Error ? error.name : "";
  if (
    name === "NotAllowedError" ||
    name === "AbortError" ||
    (error instanceof Error &&
      /cancelled|canceled|timed out/i.test(error.message))
  )
    return "The passkey request was cancelled or timed out. You can try again when you're ready.";
  if (name === "InvalidStateError")
    return "This device may already have a passkey for your account. Try signing in instead.";
  if (name === "SecurityError")
    return "Passkeys require a secure connection and the workspace's registered hostname. Open your usual workspace address to continue.";
  return errorMessage(error);
}

function returnPath(value: string | null) {
  if (
    !value ||
    !/^\/(approvals|tasks)(\/|\?|$)/.test(value) ||
    value.includes("\\")
  )
    return "/approvals";
  return value;
}

export function AuthScreen({ mode }: { mode: "login" | "register" }) {
  const params = useSearchParams();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [label, setLabel] = useState("This device");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [registered, setRegistered] = useState(false);
  const [registration, setRegistration] = useState<Registration | null>(null);
  const invite = params.get("invite");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (!browserSupportsWebAuthn()) {
      setError(
        "This browser doesn't support passkeys. Open this workspace in a recent version of Chrome, Safari, Edge or Firefox.",
      );
      return;
    }
    setBusy(true);
    try {
      if (mode === "register") {
        if (!invite)
          throw new Error(
            "This invitation link is missing its token. Ask your workspace owner for a new invitation.",
          );
        // A cancelled platform prompt can reuse its unexpired challenge without consuming the invite again.
        const challenge =
          registration ??
          (await api<Registration>("/auth/passkey/register/begin", {
            method: "POST",
            body: JSON.stringify({ invite }),
          }));
        setRegistration(challenge);
        const credential = await startRegistration({
          optionsJSON: challenge.options,
        });
        await api("/auth/passkey/register/finish", {
          method: "POST",
          body: JSON.stringify({
            challenge_id: challenge.challenge_id,
            credential,
            label: label.trim(),
          }),
        });
        setRegistered(true);
      } else {
        const challenge = await api<Authentication>(
          "/auth/passkey/login/begin",
          { method: "POST", body: JSON.stringify({ email: email.trim() }) },
        );
        const credential = await startAuthentication({
          optionsJSON: challenge.options,
        });
        await api<User>("/auth/passkey/login/finish", {
          method: "POST",
          body: JSON.stringify({
            challenge_id: challenge.challenge_id,
            credential,
          }),
        });
        router.replace(returnPath(params.get("next")));
        router.refresh();
      }
    } catch (err) {
      setError(
        mode === "register" && err instanceof ApiError && err.status === 401
          ? "This invitation has expired or has already been used. Ask your workspace owner for a new invitation."
          : friendlyAuthError(err),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="auth-layout">
      <aside className="auth-story">
        <Link href="/auth/login" className="brand">
          <Mark />
          <span>
            BIZ<span className="brand-light">OS</span>
          </span>
        </Link>
        <div className="auth-story-copy">
          <span className="auth-tag">
            <span className="live-dot" />
            YOUR BUSINESS, IN VIEW
          </span>
          <h1>
            The work moves.
            <br />
            You stay in control.
          </h1>
          <p>
            A calm place to review decisions, follow progress and see the record
            behind every result.
          </p>
          <div className="auth-illustration" aria-hidden="true">
            <div className="illustration-line" />
            <div className="illustration-card">
              <span className="illustration-icon">
                <ShieldCheck size={24} />
              </span>
              <div>
                <strong>A decision, with context.</strong>
                <span>Review · Approve · Follow through</span>
              </div>
              <span className="illustration-check">
                <Check size={17} />
              </span>
            </div>
            <div className="illustration-card secondary">
              <span className="illustration-icon">
                <Check size={20} />
              </span>
              <div>
                <strong>Every step, accounted for.</strong>
                <span>Connected work. A clear record.</span>
              </div>
            </div>
          </div>
        </div>
        <p className="auth-story-footer">
          Built for the way your business works.
        </p>
      </aside>
      <main id="main-content" className="auth-main">
        <div className="auth-mobile-brand">
          <Mark />
          <strong>BIZ OS</strong>
        </div>
        <div className="auth-form-wrap">
          <span className={`auth-form-icon ${registered ? "is-success" : ""}`}>
            {registered ? (
              <Check size={27} />
            ) : mode === "register" ? (
              <KeyRound size={27} />
            ) : (
              <Fingerprint size={30} />
            )}
          </span>
          <p className="eyebrow">
            {registered
              ? "YOU'RE READY"
              : mode === "register"
                ? "YOUR WORKSPACE INVITATION"
                : "WELCOME BACK"}
          </p>
          <h2>
            {registered
              ? "Passkey created"
              : mode === "register"
                ? "Make yourself at home."
                : "Sign in to your workspace."}
          </h2>
          <p className="auth-description">
            {registered
              ? "Your passkey is saved. Sign in to open your workspace."
              : mode === "register"
                ? "Set up a passkey to securely access your workspace. Your device will guide you through it."
                : "Use your passkey to pick up where you left off."}
          </p>
          {error && <ErrorNotice>{error}</ErrorNotice>}
          {registered ? (
            <Link
              className="button button-primary full-width"
              href="/auth/login"
            >
              Continue to sign in
              <ArrowRight size={17} />
            </Link>
          ) : (
            <form onSubmit={(event) => void submit(event)}>
              {mode === "login" ? (
                <div className="form-field">
                  <label htmlFor="email">Email address</label>
                  <input
                    id="email"
                    type="email"
                    autoComplete="username webauthn"
                    placeholder="you@yourbusiness.com"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                    disabled={busy}
                  />
                  <p className="field-hint">
                    Use the email address you registered with.
                  </p>
                </div>
              ) : (
                <>
                  <div className="form-field">
                    <label htmlFor="passkey-label">Passkey name</label>
                    <input
                      id="passkey-label"
                      value={label}
                      onChange={(event) => setLabel(event.target.value)}
                      maxLength={120}
                      autoComplete="off"
                      disabled={busy}
                    />
                    <p className="field-hint">
                      A name to help you recognise this passkey later.
                    </p>
                  </div>
                  {!invite && (
                    <ErrorNotice>
                      This invitation link is incomplete. Ask your workspace
                      owner for a new invitation.
                    </ErrorNotice>
                  )}
                </>
              )}
              <button
                className="button button-primary full-width auth-submit"
                type="submit"
                disabled={busy || (mode === "register" && !invite)}
              >
                {busy ? (
                  <LoaderCircle size={19} className="spin" />
                ) : (
                  <Fingerprint size={19} />
                )}
                {busy
                  ? "Follow your device's prompt…"
                  : mode === "register"
                    ? "Create passkey"
                    : "Sign in with passkey"}
                {!busy && <ArrowRight size={17} />}
              </button>
            </form>
          )}
          <div className="auth-security">
            <ShieldCheck size={16} />
            <span>
              {mode === "register" && !registered
                ? "Keep this page open until your passkey is saved."
                : "Protected by your device. No password to remember."}
            </span>
          </div>
          {mode === "register" && !registered ? (
            <p className="auth-help">
              Already have a passkey? <Link href="/auth/login">Sign in</Link>
            </p>
          ) : (
            !registered && (
              <p className="auth-help">
                New here? Open the invitation from your workspace owner.
              </p>
            )
          )}
        </div>
        <p className="auth-bottom">
          BIZ OS <span>·</span> Your business, in view.
        </p>
      </main>
    </div>
  );
}
