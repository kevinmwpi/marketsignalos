import { NextResponse } from "next/server";

export async function GET() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${apiBase}/ingestor/status`, { cache: "no-store" });
    const data = await res.json().catch(() => null);
    if (data === null) return NextResponse.json({ detail: "Bad response from API" }, { status: 502 });
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "API unreachable" },
      { status: 502 },
    );
  }
}
