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

  await test.step("signing out revokes browser access", async () => {
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/auth\/login/);
    expect((await page.request.get("/v1/auth/me")).status()).toBe(401);
    await page.goto(`/tasks/${state.operations.approve.task_id}`);
    await expect(page).toHaveURL(/\/auth\/login/);
  });
});
