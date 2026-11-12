import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

type Operation = {
  id: string;
  task_id: string;
  version: number;
  inputs: { to: string; subject: string; body: string; idempotency_key: string };
};

type State = {
  owner: { email: string; invite_url: string };
  operations: Record<"approve" | "reject" | "stale", Operation>;
  sends: string;
};

test("owner enrols a passkey, reviews exact approvals and sees evidence-bound completion", async ({
  page,
  context,
}) => {
  const state = JSON.parse(
    fs.readFileSync(path.resolve("test-results/browser-state.json"), "utf8"),
  ) as State;
  const origin = new URL(state.owner.invite_url).origin;
  const sends = () =>
    fs.existsSync(state.sends)
      ? fs.readFileSync(state.sends, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line))
      : [];
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

  await test.step("signed-out routes are protected by the real API", async () => {
    await page.goto("/approvals");
    await expect(page).toHaveURL(/\/auth\/login/);
    expect((await page.request.get("/v1/approvals")).status()).toBe(401);
    expect((await page.request.get("/v1/tasks")).status()).toBe(401);
  });

  await test.step("fresh owner invite enrols and authenticates a real virtual passkey", async () => {
    await page.goto(state.owner.invite_url);
    const registration = page.waitForResponse("**/v1/auth/passkey/register/finish");
    await page.getByRole("button", { name: "Create passkey" }).click();
    expect((await registration).status()).toBe(201);
    await expect(page.getByRole("heading", { name: "Passkey created" })).toBeVisible();
    await page.getByRole("link", { name: "Continue to sign in" }).click();
    await page.getByLabel("Email address").fill(state.owner.email);
    const login = page.waitForResponse("**/v1/auth/passkey/login/finish");
    await page.getByRole("button", { name: "Sign in with passkey" }).click();
    expect((await login).status()).toBe(200);
    await expect(page.getByRole("heading", { name: "Approval queue" })).toBeVisible();
    expect((await page.request.get("/v1/auth/me")).status()).toBe(200);
    const session = (await context.cookies()).find((cookie) => cookie.name === "biz_session");
    expect(session?.httpOnly).toBe(true);
    expect(session?.sameSite).toBe("Lax");
  });

  await test.step("review shows the exact pending message and version", async () => {
    const operation = state.operations.approve;
    await page.goto(`/approvals/${operation.id}`);
    await expect(page.getByText(operation.inputs.to, { exact: true })).toBeVisible();
    await expect(page.getByText(operation.inputs.subject, { exact: true })).toBeVisible();
    await expect(page.getByText(operation.inputs.body, { exact: true })).toBeVisible();
    await expect(page.locator("img")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Approve version 1" })).toBeVisible();
    expect(sends()).toHaveLength(0);
    const crossOrigin = await page.request.post(`/v1/approvals/${operation.id}/approve`, {
      data: { version: 1 },
      headers: { Origin: "https://outside.example.invalid" },
    });
    expect(crossOrigin.status()).toBe(403);
    expect(sends()).toHaveLength(0);
    await page.screenshot({ path: "test-results/owner-console-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/approvals");
    await expect(page.getByRole("heading", { name: "Approval queue" })).toBeVisible();
    await page.screenshot({ path: "test-results/owner-console-mobile.png", fullPage: true });
    const mobile = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      overflowing: Array.from(document.querySelectorAll("body *"))
        .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 20)
        .map((element) => ({
          tag: element.tagName,
          class: element.className,
          right: element.getBoundingClientRect().right,
        })),
    }));
    expect.soft(mobile.document, JSON.stringify(mobile)).toBeLessThanOrEqual(mobile.viewport);
    await page.setViewportSize({ width: 1440, height: 1000 });
  });

  await test.step("UI approval completes once with linked evidence", async () => {
    const operation = state.operations.approve;
    await page.goto(`/approvals/${operation.id}`);
    const approval = page.waitForResponse(`**/v1/approvals/${operation.id}/approve`);
    await page.getByRole("button", { name: "Approve version 1" }).click();
    expect((await approval).status()).toBe(200);
    await expect.poll(() => sends().length).toBe(1);
    expect(sends()[0]).toEqual({
      key: operation.inputs.idempotency_key,
      to: [operation.inputs.to],
      subject: operation.inputs.subject,
      body: operation.inputs.body,
    });
    await expect
      .poll(async () => (await (await page.request.get(`/v1/tasks/${operation.task_id}`)).json()).status)
      .toBe("done");
    await page.goto(`/tasks/${operation.task_id}`);
    await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Approval Granted", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Packet Message Sent", exact: true })).toBeVisible();
    const activity = await (await page.request.get(`/v1/tasks/${operation.task_id}/activity`)).json();
    expect(activity.map((event: { kind: string }) => event.kind)).toEqual(
      expect.arrayContaining(["approval.granted", "packet:message.sent"]),
    );
    await page.screenshot({ path: "test-results/owner-console-evidence.png", fullPage: true });
    await page.reload();
    const retry = await page.request.post(`/v1/approvals/${operation.id}/approve`, {
      data: { version: 1 },
      headers: { Origin: origin },
    });
    expect(retry.status()).toBe(409);
    expect(sends()).toHaveLength(1);
  });

  await test.step("rejection leaves its task open and sends nothing", async () => {
    const operation = state.operations.reject;
    await page.goto(`/approvals/${operation.id}`);
    const rejection = page.waitForResponse(`**/v1/approvals/${operation.id}/reject`);
    await page.getByRole("button", { name: "Reject request" }).click();
    await page.getByLabel(/Reason for rejection/).fill("Reviewed and declined in browser acceptance.");
    await page.getByRole("button", { name: "Confirm rejection" }).click();
    expect((await rejection).status()).toBe(200);
    await expect
      .poll(async () => (await (await page.request.get(`/v1/approvals/${operation.id}`)).json()).status)
      .toBe("rejected");
    const task = await (await page.request.get(`/v1/tasks/${operation.task_id}`)).json();
    expect(task.status).toBe("open");
    expect(sends()).toHaveLength(1);
  });

  await test.step("a stale card gets a real 409 and reloads the new version", async () => {
    const operation = state.operations.stale;
    await page.goto(`/approvals/${operation.id}`);
    await expect(page.getByRole("button", { name: "Approve version 1" })).toBeVisible();
    const changed = { ...operation.inputs, body: "Edited by another owner session." };
    const edit = await page.request.post(`/v1/approvals/${operation.id}/edit`, {
      data: { inputs: changed },
      headers: { Origin: origin },
    });
    expect(edit.status()).toBe(200);
    expect((await edit.json()).version).toBe(2);
    const stale = page.waitForResponse(`**/v1/approvals/${operation.id}/approve`);
    await page.getByRole("button", { name: "Approve version 1" }).click();
    expect((await stale).status()).toBe(409);
    await expect(
      page.getByRole("alert").filter({ hasText: "This request changed while you were reviewing it." }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve version 2" })).toBeVisible();
    await expect(page.getByText(changed.body, { exact: true })).toBeVisible();
    expect(sends()).toHaveLength(1);
  });

  const noSidewaysScroll = async () => {
    const size = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect.soft(size.document, JSON.stringify(size)).toBeLessThanOrEqual(size.viewport);
  };
  // Desktop and phone screenshots of the current page, checking the phone layout fits.
  const screenshots = async (name: string) => {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: `test-results/${name}-desktop.png`, fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    // The navigation slides away when the layout narrows; let it finish.
    await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
    await expect(page.locator(".sidebar")).not.toBeInViewport();
    await page.evaluate(() => window.scrollTo(0, 0));
    await noSidewaysScroll();
    await page.screenshot({ path: `test-results/${name}-mobile.png`, fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  };
  const banner = page.getByRole("alert", { name: "Paused" });
  const edited = "Corrected in the browser: <b>plain text</b> & the right date.";
  const editReason = "The draft gave the wrong date.";

  await test.step("pausing shows the banner everywhere and blocks approval until resumed", async () => {
    const operation = state.operations.stale;
    await page.goto(`/approvals/${operation.id}`);
    await expect(page.getByRole("button", { name: "Approve version 2" })).toBeEnabled();
    await expect(banner).toHaveCount(0);

    // Paused from elsewhere while this card is open: the refusal reads as paused, not as stale.
    const elsewhere = await page.request.post("/v1/control/pause", {
      data: { reason: "Paused from another session." },
      headers: { Origin: origin },
    });
    expect((await elsewhere.json()).paused).toBe(true);
    const refused = page.waitForResponse(`**/v1/approvals/${operation.id}/approve`);
    await page.getByRole("button", { name: "Approve version 2" }).click();
    const refusal = await refused;
    expect(refusal.status()).toBe(409);
    expect((await refusal.json()).detail).toMatch(/^paused/);
    await expect(
      page.getByRole("alert").filter({ hasText: "Everything is paused, so this was not approved." }),
    ).toBeVisible();
    await expect(page.getByText("This request changed while you were reviewing it.")).toHaveCount(0);
    await expect(banner).toContainText("Paused from another session.");
    await banner.getByRole("button", { name: "Resume" }).click();
    await expect(banner).toHaveCount(0);

    // Paused from the header, with one confirmation and a reason.
    await page.getByRole("button", { name: "Pause everything" }).click();
    const dialog = page.getByRole("dialog", { name: "Pause everything?" });
    await expect(dialog).toBeVisible();
    expect((await (await page.request.get("/v1/control")).json()).paused).toBe(false);
    await dialog.getByLabel(/Reason/).fill("Checking a wrong reply <i>first</i>.");
    await dialog.getByRole("button", { name: "Pause everything now" }).click();
    await expect(banner).toContainText("Everything is paused");
    await expect(banner).toContainText("Paused by the owner on");
    await expect(banner).toContainText("Reason: Checking a wrong reply <i>first</i>.");
    await expect(banner.locator("i")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Approve version 2" })).toBeDisabled();
    await expect(page.getByText("Everything is paused, so nothing can be approved.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Edit proposal" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Reject request" })).toBeEnabled();
    const blocked = await page.request.post(`/v1/approvals/${operation.id}/approve`, {
      data: { version: 2 },
      headers: { Origin: origin },
    });
    expect(blocked.status()).toBe(409);
    expect((await blocked.json()).detail).toMatch(/^paused/);
    expect(sends()).toHaveLength(1);
    await screenshots("owner-console-paused");
    for (const path of ["/approvals", "/tasks", "/scorecard"]) {
      await page.goto(path);
      await expect(banner).toContainText("Everything is paused");
    }

    await page.goto(`/approvals/${operation.id}`);
    await banner.getByRole("button", { name: "Resume" }).click();
    await expect(banner).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Approve version 2" })).toBeEnabled();
    expect((await (await page.request.get("/v1/control")).json()).paused).toBe(false);
    expect(sends()).toHaveLength(1);
  });

  await test.step("an edit with a reason and severity becomes the version that is approved and sent", async () => {
    const operation = state.operations.stale;
    await page.goto(`/approvals/${operation.id}`);
    await page.getByRole("button", { name: "Edit proposal" }).click();
    await expect(page.getByLabel("Body")).toHaveValue("Edited by another owner session.");
    await expect(page.locator("#edit-to")).toHaveValue(operation.inputs.to);
    await expect(page.locator("#edit-idempotency_key")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Approve version/ })).toHaveCount(0);

    // Reason and severity are both required before anything is sent to the API.
    let editCalls = 0;
    page.on("request", (request) => {
      if (request.url().endsWith(`/v1/approvals/${operation.id}/edit`)) editCalls += 1;
    });
    await page.getByLabel("Body").fill(edited);
    await page.getByRole("button", { name: "Save as version 3" }).click();
    await expect(page.getByText("Say in one line why you changed it.")).toBeVisible();
    await page.getByLabel("Reason, in one line").fill(editReason);
    await page.getByRole("button", { name: "Save as version 3" }).click();
    await expect(page.getByText("Choose what kind of change this was.")).toBeVisible();
    expect(editCalls).toBe(0);
    await expect(page.getByRole("radio")).toHaveCount(3);
    await page.getByRole("radio", { name: /A fact was wrong/ }).check();
    await screenshots("owner-console-edit");

    const saving = page.waitForResponse(`**/v1/approvals/${operation.id}/edit`);
    await page.getByRole("button", { name: "Save as version 3" }).click();
    const saved = await saving;
    expect(saved.status()).toBe(200);
    expect(saved.request().postDataJSON()).toEqual({
      inputs: { ...operation.inputs, body: edited },
      reason: editReason,
      severity: "factual",
    });
    const card = await saved.json();
    expect(card.version).toBe(3);
    expect(card.status).toBe("pending");
    expect(editCalls).toBe(1);
    await expect(page.getByRole("status").filter({ hasText: "saved as version 3" })).toBeVisible();
    await expect(page.getByText(edited, { exact: true })).toBeVisible();
    await expect(page.locator("main b")).toHaveCount(0);
    expect(sends()).toHaveLength(1);

    const approval = page.waitForResponse(`**/v1/approvals/${operation.id}/approve`);
    await page.getByRole("button", { name: "Approve version 3" }).click();
    const approved = await approval;
    expect(approved.status()).toBe(200);
    expect(approved.request().postDataJSON()).toEqual({ version: 3 });
    await expect.poll(() => sends().length).toBe(2);
    expect(sends()[1]).toEqual({
      key: operation.inputs.idempotency_key,
      to: [operation.inputs.to],
      subject: operation.inputs.subject,
      body: edited,
    });
  });

  await test.step("the scorecard shows the measured record and the correction", async () => {
    await page.goto("/scorecard");
    await expect(page.getByRole("heading", { name: "Scorecard" })).toBeVisible();
    const report = await (await page.request.get("/v1/metrics?days=28")).json();
    const totals = { proposals: 0, approved_unchanged: 0, approved_with_errors: 0, rejected: 0 };
    for (const row of report.decision_types)
      for (const key of Object.keys(totals) as (keyof typeof totals)[]) totals[key] += row[key];
    expect(totals).toEqual({ proposals: 3, approved_unchanged: 1, approved_with_errors: 1, rejected: 1 });
    const table = page.getByRole("region", { name: "Results by role and type of decision" });
    await expect(table.getByRole("row")).toHaveCount(report.decision_types.length + 1);
    for (const heading of [
      "Proposed",
      "Approved as is",
      "Wording-only edits",
      "Approved with errors",
      "Rejected",
      "Waiting",
      "Approved as is rate",
      "Error-free rate",
    ])
      await expect(table.getByRole("columnheader", { name: heading, exact: true })).toBeVisible();
    for (const label of ["Tasks completed", "Serious errors", "Cost per task", "Time saved"])
      await expect(page.getByText(label, { exact: true })).toBeVisible();

    const corrections = await (await page.request.get("/v1/corrections?limit=20")).json();
    expect(corrections[0]).toMatchObject({ reason: editReason, severity: "factual", to_version: 3 });
    const latest = page.locator(".correction").first();
    await expect(latest).toContainText(editReason);
    await expect(latest).toContainText("A fact was wrong");
    await expect(latest.getByText("Edited by another owner session.", { exact: true })).toBeVisible();
    await expect(latest.getByText(edited, { exact: true })).toBeVisible();
    await expect(page.locator("main b")).toHaveCount(0);
    await screenshots("owner-console-scorecard");

    // A missing measurement is said in words. Only the page's own read is replaced here.
    await page.route("**/v1/metrics?days=28", (route) =>
      route.fulfill({
        json: {
          ...report,
          decision_types: [
            {
              ...report.decision_types[0],
              proposals: 2,
              approved_unchanged: 0,
              approved_cosmetic: 0,
              approved_with_errors: 0,
              rejected: 1,
              pending: 1,
              approval_without_edit_rate: null,
              error_free_rate: null,
            },
          ],
          tasks_completed: 0,
          cost_per_task_usd: null,
          time_saved_minutes: null,
        },
      }),
    );
    await page.reload();
    await expect(page.getByText("Not measured yet", { exact: true })).toHaveCount(4);
    await expect(table.getByRole("row").nth(1)).not.toContainText("%");
    await page.screenshot({ path: "test-results/owner-console-scorecard-unmeasured.png", fullPage: true });
    await page.unroute("**/v1/metrics?days=28");
  });

  await test.step("signing out revokes browser access", async () => {
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/auth\/login/);
    expect((await page.request.get("/v1/auth/me")).status()).toBe(401);
    await page.goto(`/tasks/${state.operations.approve.task_id}`);
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
