import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import type {
  TrainingCandidate,
  TrainingCohort,
  TrainingComparison,
  TrainingVersion,
} from "../src/lib/training";

type State = {
  training_owner: { email: string; invite_url: string };
  training_candidate_id: string;
  training_correction_id: string;
};

test("owner versions, attests and queues private training without activating instructions", async ({
  page,
  context,
}) => {
  const state = JSON.parse(
    fs.readFileSync(path.resolve("test-results/browser-state.json"), "utf8"),
  ) as State;
  const authenticator = await context.newCDPSession(page);
  await authenticator.send("WebAuthn.enable");
  await authenticator.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  await page.goto(state.training_owner.invite_url);
  await page
    .getByRole("button", { name: "Create passkey", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Passkey created", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Continue to sign in", exact: true })
    .click();
  await page.getByLabel("Email address").fill(state.training_owner.email);
  await page
    .getByRole("button", { name: "Sign in with passkey", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Approval queue", exact: true }),
  ).toBeVisible();
  // The owner-console acceptance also proves staff can pause; this owner restores
  // the shared disposable fixture before recording version-specific reviews.
  if ((await (await page.request.get("/v1/control")).json()).paused) {
    await page.getByRole("button", { name: "Resume", exact: true }).click();
    await expect
      .poll(
        async () =>
          (await (await page.request.get("/v1/control")).json()).paused,
      )
      .toBe(false);
  }
  const candidate = (await (
    await page.request.get(
      `/v1/training/candidates/${state.training_candidate_id}`,
    )
  ).json()) as TrainingCandidate;
  let first: TrainingVersion;
  let current: TrainingVersion;
  let cohort: TrainingCohort;
  let job: TrainingComparison;

  await test.step("create a version with a separate review from its root", async () => {
    await page.goto(`/training/${candidate.id}`);
    await page
      .getByRole("link", {
        name: "Candidate versions and local comparisons",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("heading", {
        name: "Candidate version history",
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByLabel("Version rule", { exact: true })
      .fill("Preserve the recorded facts when preparing a reply.");
    const initial = await page.request.post(
      `/v1/training/candidates/${candidate.id}/versions`,
      {
        data: {
          parent_id: null,
          rule: "Another owner saved the initial version first.",
          system: await page
            .getByLabel("Candidate instructions", { exact: true })
            .inputValue(),
          expected_output: candidate.expected_output,
          snapshot_id: candidate.snapshot_id,
          checks: candidate.regression_case?.checks,
        },
      },
    );
    expect(initial.ok()).toBe(true);
    const firstConcurrent = (await initial.json()) as TrainingVersion;
    const initialConflict = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/training/candidates/${candidate.id}/versions`) &&
        response.status() === 409,
    );
    await page
      .getByRole("button", { name: "Save new version", exact: true })
      .click();
    await initialConflict;
    await expect(page.getByLabel("Version rule", { exact: true })).toHaveValue(
      "Preserve the recorded facts when preparing a reply.",
    );
    await page
      .getByText("Review latest version before keeping your edits", {
        exact: true,
      })
      .click();
    await page
      .getByRole("button", {
        name: `Use version ${firstConcurrent.number} as parent`,
        exact: true,
      })
      .click();
    const saved = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/training/candidates/${candidate.id}/versions`) &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Save new version", exact: true })
      .click();
    const response = await saved;
    expect(response.ok()).toBe(true);
    expect(response.headers()["cache-control"]).toContain("no-store");
    first = (await response.json()) as TrainingVersion;
    await expect(page).toHaveURL(new RegExp(`/versions/${first.id}$`));
    await expect(
      page.getByRole("heading", {
        name: "Review this exact version",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Queue local comparison", exact: true }),
    ).toHaveCount(0);
    expect(first.status).toBe("proposed");
    const refused = await page.request.post("/v1/training/cohorts", {
      data: { name: "Unreviewed version cannot run", version_ids: [first.id] },
    });
    expect([409, 422]).toContain(refused.status());
    await page
      .getByLabel("Version review note", { exact: true })
      .fill("This version needs a successor before testing.");
    await page
      .getByRole("button", { name: "Reject version", exact: true })
      .click();
    await expect(
      page.getByText("This version is rejected. Its history stays available.", {
        exact: true,
      }),
    ).toBeVisible();
  });

  await test.step("stale-parent conflict preserves edits and requires an explicit rebase", async () => {
    await page
      .getByText("Create a successor without rewriting this version", {
        exact: true,
      })
      .click();
    const unsaved = "Use verified facts only. <img src=x onerror=alert(1)>";
    await page.getByLabel("Version rule", { exact: true }).fill(unsaved);
    const expectedText = JSON.stringify(
      { ...first.expected_output, body: "Exact successor expectation." },
      null,
      2,
    );
    await page
      .getByLabel("Expected target result (JSON)", { exact: true })
      .fill(expectedText);
    const system = await page
      .getByLabel("Candidate instructions", { exact: true })
      .inputValue();
    const concurrentResponse = await page.request.post(
      `/v1/training/candidates/${candidate.id}/versions`,
      {
        data: {
          parent_id: first.id,
          rule: "Concurrent owner's version",
          system: `${system}\nConcurrent amendment.`,
          expected_output: first.expected_output,
          snapshot_id: first.snapshot_id,
          checks: first.checks,
        },
      },
    );
    expect(concurrentResponse.ok()).toBe(true);
    const concurrent = (await concurrentResponse.json()) as TrainingVersion;
    const conflict = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/training/candidates/${candidate.id}/versions`) &&
        response.status() === 409,
    );
    await page
      .getByRole("button", { name: "Save new version", exact: true })
      .click();
    await conflict;
    await expect(page.locator("main").getByRole("alert")).toContainText(
      "Your unsaved rule, instructions and expected result are kept",
    );
    await expect(page.getByLabel("Version rule", { exact: true })).toHaveValue(
      unsaved,
    );
    await expect(
      page.getByLabel("Candidate instructions", { exact: true }),
    ).toHaveValue(system);
    await expect(
      page.getByLabel("Expected target result (JSON)", { exact: true }),
    ).toHaveValue(expectedText);
    await expect(
      page.getByRole("button", { name: "Save new version", exact: true }),
    ).toBeDisabled();
    await page
      .getByText("Review latest version before keeping your edits", {
        exact: true,
      })
      .click();
    await page
      .getByRole("button", {
        name: `Use version ${concurrent.number} as parent`,
        exact: true,
      })
      .click();
    await expect(page.getByLabel("Version rule", { exact: true })).toHaveValue(
      unsaved,
    );
    const saved = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/training/candidates/${candidate.id}/versions`) &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Save new version", exact: true })
      .click();
    current = (await (await saved).json()) as TrainingVersion;
    expect(current.parent_id).toBe(concurrent.id);
    expect(current.status).toBe("proposed");
    await expect(page).toHaveURL(new RegExp(`/versions/${current.id}$`));
    await expect(
      page.getByRole("button", { name: "Queue local comparison", exact: true }),
    ).toHaveCount(0);
    await expect(page.locator("main img")).toHaveCount(0);
    const old = (await (
      await page.request.get(`/v1/training/versions/${first.id}`)
    ).json()) as TrainingVersion;
    expect(old.status).toBe("rejected");
    expect(old.rule).toBe(first.rule);
    await page
      .getByLabel("Version review note", { exact: true })
      .fill("Review of this exact successor for private local testing.");
    await page
      .getByRole("button", { name: "Accept version for testing", exact: true })
      .click();
    await expect(
      page.getByRole("heading", {
        name: "Freeze a private tuning cohort",
        exact: true,
      }),
    ).toBeVisible();
    await page.screenshot({
      path: "test-results/training-version-desktop.png",
      fullPage: true,
      animations: "disabled",
    });
  });

  await test.step("cohort attestation freezes membership and expected checks", async () => {
    await page
      .getByLabel("Cohort name", { exact: true })
      .fill("Private browser tuning cohort");
    const saved = page.waitForResponse(
      (response) =>
        response.url().endsWith("/training/cohorts") &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", {
        name: "Freeze selected tuning cases",
        exact: true,
      })
      .click();
    const response = await saved;
    expect(response.ok()).toBe(true);
    cohort = (await response.json()) as TrainingCohort;
    expect(cohort.members).toHaveLength(1);
    expect(cohort.status).toBe("proposed");
    await page
      .getByRole("link", { name: "Review frozen cohort", exact: true })
      .click();
    await expect(
      page.getByRole("heading", {
        name: "Owner tuning attestation",
        exact: true,
      }),
    ).toBeVisible();
    await page.screenshot({
      path: "test-results/training-cohort-desktop.png",
      fullPage: true,
      animations: "disabled",
    });
    await expect(
      page.getByRole("button", {
        name: "Attest this frozen cohort for testing",
        exact: true,
      }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Queue local comparison", exact: true }),
    ).toHaveCount(0);
    await page
      .getByLabel(
        "I reviewed this frozen tuning cohort and its expected results.",
        { exact: true },
      )
      .check();
    await page
      .getByLabel("Attestation note", { exact: true })
      .fill(
        "Exact tuning members and checks reviewed; no held-out readiness claim.",
      );
    await page
      .getByRole("button", {
        name: "Attest this frozen cohort for testing",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("heading", {
        name: "Queue a paired local comparison",
        exact: true,
      }),
    ).toBeVisible();
    const frozen = (await (
      await page.request.get(`/v1/training/cohorts/${cohort.id}`)
    ).json()) as TrainingCohort;
    expect(frozen.payload_sha256).toBe(cohort.payload_sha256);
    expect(frozen.members).toEqual(cohort.members);
  });

  await test.step("queue promptly and poll durable job state", async () => {
    await page
      .getByLabel("Installed model name", { exact: true })
      .fill("llama3.1:8b");
    await page
      .getByLabel("Installed model digest", { exact: true })
      .fill(`sha256:${"a".repeat(64)}`);
    await page.getByLabel("Ollama version", { exact: true }).fill("0.20.0");
    await page
      .getByLabel("Model adapter version", { exact: true })
      .fill("1.0.0");
    const queued = page.waitForResponse(
      (response) =>
        response.url().endsWith("/training/comparisons") &&
        response.request().method() === "POST",
    );
    const start = Date.now();
    await page
      .getByRole("button", { name: "Queue local comparison", exact: true })
      .click();
    const response = await queued;
    expect(response.ok()).toBe(true);
    expect(Date.now() - start).toBeLessThan(20000);
    expect(response.headers()["cache-control"]).toContain("no-store");
    job = (await response.json()) as TrainingComparison;
    expect(job.status).toBe("queued");
    await expect(page).toHaveURL(
      new RegExp(`/training/comparisons/${job.id}$`),
    );
    await expect(
      page.getByRole("heading", {
        name: "Training comparison evidence",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByRole("status")).toContainText(
      "Queued for the local host worker.",
    );
    let polls = 0;
    page.on("request", (request) => {
      if (
        request.method() === "GET" &&
        request.url().endsWith(`/training/comparisons/${job.id}`)
      )
        polls += 1;
    });
    await expect.poll(() => polls).toBeGreaterThan(0);
  });

  await test.step("frontend result fixture shows regressions, unknowns and escaped private outputs", async () => {
    const unsafe = "<img src=x onerror=alert(1)> private fixture";
    const result = (
      side: "baseline" | "candidate",
      caseId: string,
      passed: boolean,
    ) => ({
      side,
      case_id: caseId,
      repetition: 1,
      status: "completed",
      passed,
      score: passed ? 1 : 0,
      failures: passed ? [] : ["Expected outcome did not match"],
      output: { body: unsafe },
      latency_ms: 123.4,
      api_cost_usd: "0",
      usage: { input_tokens: 12, output_tokens: 8 },
      hardware_cost_usd: null,
    });
    const fixture: TrainingComparison = {
      ...job,
      status: "failed",
      events: [],
      summary: {
        valid: false,
        verdict: "invalid",
        baseline_passed: 1,
        candidate_passed: 1,
        total_cases: 3,
        regressions: [{ case_id: "older", repetition: 1 }],
        target_outcomes: [
          {
            case_id: "target",
            repetition: 1,
            baseline_passed: false,
            candidate_passed: true,
          },
        ],
        errors: [
          {
            case_id: "unknown",
            side: "baseline",
            repetition: 1,
            error: "Inference outcome is unknown after interruption",
          },
        ],
        missing: [{ case_id: "unknown", side: "candidate", repetition: 1 }],
        results: [
          result("baseline", "target", false),
          result("candidate", "target", true),
          result("baseline", "older", true),
          result("candidate", "older", false),
          {
            side: "baseline",
            case_id: "unknown",
            repetition: 1,
            status: "unknown",
            error: "Inference outcome is unknown after interruption",
            output: null,
            api_cost_usd: null,
          },
        ],
        known_api_cost_usd: "0",
        unmeasured_api_cost_results: 1,
        hardware_cost_usd: null,
        electricity_cost_usd: null,
      },
    };
    await page.route(`**/v1/training/comparisons/${job.id}`, (route) =>
      route.fulfill({
        json: fixture,
        headers: { "Cache-Control": "no-store" },
      }),
    );
    await page.reload();
    await expect(
      page.getByText("Invalid comparison — no result is established", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByText("1 regression requires review", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Case older, repetition 1" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Case unknown, repetition 1" }),
    ).toContainText("Missing result");
    await expect(page.locator("main img")).toHaveCount(0);
    await page.screenshot({
      path: "test-results/training-comparison-desktop.png",
      fullPage: true,
      animations: "disabled",
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).not.toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: "test-results/training-comparison-mobile.png",
      fullPage: true,
      animations: "disabled",
    });
    await page.unroute(`**/v1/training/comparisons/${job.id}`);
  });

  await test.step("frontend fixture hides retained success after a source-integrity refusal", async () => {
    let refused = false;
    const marker = "Private retained successful evidence";
    const fixture: TrainingComparison = {
      ...job,
      status: "completed",
      summary: {
        valid: true,
        verdict: "improved",
        baseline_passed: 0,
        candidate_passed: 1,
        total_cases: 1,
        regressions: [],
        errors: [],
        missing: [],
        target_outcomes: [],
        results: [
          {
            side: "baseline",
            case_id: "target",
            repetition: 1,
            status: "completed",
            passed: false,
            failures: ["Wrong prior outcome"],
            output: { body: marker },
          },
          {
            side: "candidate",
            case_id: "target",
            repetition: 1,
            status: "completed",
            passed: true,
            output: { body: marker },
          },
        ],
      },
    };
    await page.route(`**/v1/training/comparisons/${job.id}`, (route) =>
      route.fulfill(
        refused
          ? {
              status: 409,
              json: { detail: "comparison integrity check failed" },
              headers: { "Cache-Control": "no-store" },
            }
          : { json: fixture, headers: { "Cache-Control": "no-store" } },
      ),
    );
    await page.reload();
    await expect(
      page.getByText("Tuning result: Improved", { exact: true }),
    ).toBeVisible();
    refused = true;
    await page
      .getByRole("button", { name: "Refresh comparison", exact: true })
      .click();
    await expect(page.locator("main").getByRole("alert")).toContainText(
      "comparison integrity check failed",
    );
    await expect(
      page.getByText("Tuning result: Improved", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", {
        name: "Frozen setup and provenance",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(page.getByText(marker, { exact: false })).toHaveCount(0);
    await expect(
      page.getByText("Comparison evidence could not be verified.", {
        exact: true,
      }),
    ).toBeVisible();
    refused = false;
    await page
      .getByRole("button", { name: "Refresh comparison", exact: true })
      .click();
    await expect(
      page.getByText("Tuning result: Improved", { exact: true }),
    ).toBeVisible();
    await page.unroute(`**/v1/training/comparisons/${job.id}`);
  });

  await test.step("private evidence stays unavailable after sign out", async () => {
    await context.clearCookies();
    expect(
      (await page.request.get(`/v1/training/comparisons/${job.id}`)).status(),
    ).toBe(401);
    await page.goto(`/training/comparisons/${job.id}`);
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
