import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
async function proxy(request: NextRequest) {
  try {
    const upstream = new URL(
      process.env.BIZ_API_URL || "http://127.0.0.1:8000",
    );
    if (
      !["http:", "https:"].includes(upstream.protocol) ||
      upstream.username ||
      upstream.password ||
      upstream.pathname !== "/" ||
      upstream.search ||
      upstream.hash
    )
      throw new Error("Invalid API origin");
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      const origin = request.headers.get("origin");
      const site = request.headers.get("sec-fetch-site");
      const expectedOrigin = new URL(
        process.env.BIZ_ORIGIN || request.nextUrl.origin,
      ).origin;
      if (
        (origin && origin !== expectedOrigin) ||
        (site && !["same-origin", "none"].includes(site))
      )
        return Response.json(
          { detail: "This request must come from your workspace." },
          { status: 403, headers: { "Cache-Control": "no-store" } },
        );
    }
    const target = new URL(
      request.nextUrl.pathname + request.nextUrl.search,
      upstream,
    );
    const headers = new Headers();
    for (const name of [
      "cookie",
      "content-type",
      "accept",
      "origin",
      "sec-fetch-site",
      "sec-fetch-mode",
      "sec-fetch-dest",
    ]) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method)
        ? undefined
        : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(25000),
    });
    const outgoing = new Headers();
    for (const name of [
      "content-type",
      "content-language",
      "location",
      "retry-after",
    ]) {
      const value = response.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    for (const cookie of response.headers.getSetCookie())
      outgoing.append("set-cookie", cookie);
    outgoing.set("cache-control", "no-store");
    outgoing.set("vary", "Cookie");
    return new Response(response.body, {
      status: response.status,
      headers: outgoing,
    });
  } catch (error) {
    console.error(
      "BIZ OS API proxy failed:",
      error instanceof Error ? error.message : "Unknown error",
    );
    return Response.json(
      {
        detail:
          "The workspace service is unavailable. Please try again shortly.",
      },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
export {
  proxy as GET,
  proxy as POST,
  proxy as PUT,
  proxy as PATCH,
  proxy as DELETE,
  proxy as OPTIONS,
  proxy as HEAD,
};
