"use client";

import { useState } from "react";

type FormState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "ok"; message: string }
  | { kind: "error"; message: string };

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

/**
 * Manual watchlist entry: paste a wallet address, it gets appended to the
 * ingestor watchlist and pinned (never archived). The wallet is hydrated
 * and scored on the next pipeline run.
 */
export default function WatchlistForm() {
  const [address, setAddress] = useState("");
  const [state, setState] = useState<FormState>({ kind: "idle" });

  async function submit() {
    const trimmed = address.trim();
    if (!ADDRESS_RE.test(trimmed)) {
      setState({
        kind: "error",
        message: "Enter a 0x-prefixed 40-hex wallet address",
      });
      return;
    }
    setState({ kind: "submitting" });
    try {
      const res = await fetch("/api/ingestor/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: trimmed }),
      });
      const data = (await res.json().catch(() => ({}))) as {
        added?: boolean;
        watchlist_size?: number;
        detail?: string;
      };
      if (!res.ok) {
        setState({
          kind: "error",
          message: data.detail ?? `Request failed (${res.status})`,
        });
        return;
      }
      setAddress("");
      setState({
        kind: "ok",
        message: data.added
          ? `Added and pinned — scored on the next ingest (${data.watchlist_size ?? "?"} watched)`
          : "Already on the watchlist — pin re-asserted",
      });
    } catch {
      setState({ kind: "error", message: "API unreachable" });
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <input
          className="w-64 rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-xs text-zinc-900 placeholder:text-zinc-400 focus:border-zinc-500 focus:outline-none"
          onChange={(event) => {
            setAddress(event.target.value);
            if (state.kind !== "idle") setState({ kind: "idle" });
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") void submit();
          }}
          placeholder="0x… wallet to watch"
          value={address}
        />
        <button
          className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-xs font-semibold text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50"
          disabled={state.kind === "submitting" || !address.trim()}
          onClick={() => void submit()}
          type="button"
        >
          {state.kind === "submitting" ? "Adding…" : "Watch wallet"}
        </button>
      </div>
      {state.kind === "ok" && (
        <p className="text-[11px] text-emerald-700">{state.message}</p>
      )}
      {state.kind === "error" && (
        <p className="text-[11px] text-red-700">{state.message}</p>
      )}
    </div>
  );
}
