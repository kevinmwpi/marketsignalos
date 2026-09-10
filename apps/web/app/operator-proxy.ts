import { NextResponse } from "next/server";

// Forward only credentials supplied by the caller. Never attach an operator
// secret from server environment to an unauthenticated public request.
export async function operatorProxy(request: Request, path: string, method: "GET" | "POST") {
  const apiBase = process.env.API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
  try {
    const res = await fetch(`${apiBase.replace(/\/$/, "")}${path}`, {
      method,
      cache: "no-store",
      headers: { Authorization: request.headers.get("authorization") ?? "" },
      signal: AbortSignal.timeout(10_000),
    });
    const data = await res.json().catch(() => ({ detail: "Bad response from API" }));
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ detail: "API unreachable" }, { status: 502 });
  }
}
