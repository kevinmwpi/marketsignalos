"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type IngestorStatus = {
  running: boolean;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_exit_code: number | null;
  last_error: string | null;
};

type ButtonState = "idle" | "starting" | "running" | "done" | "error";

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

export default function IngestButton() {
  const [buttonState, setButtonState] = useState<ButtonState>("idle");
  const [status, setStatus] = useState<IngestorStatus | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const fetchStatus = useCallback(async (): Promise<IngestorStatus | null> => {
    try {
      const res = await fetch("/api/ingestor/status", { cache: "no-store" });
      if (!res.ok) return null;
      return (await res.json()) as IngestorStatus;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    fetchStatus().then((s) => {
      if (s?.running) {
        setStatus(s);
        setButtonState("running");
      }
    });
    return stopPolling;
  }, [fetchStatus, stopPolling]);

  useEffect(() => {
    if (buttonState !== "running") {
      stopPolling();
      return;
    }
    pollRef.current = setInterval(async () => {
      const s = await fetchStatus();
      if (!s) return;
      setStatus(s);
      if (!s.running) {
        stopPolling();
        if (s.last_exit_code === 0) {
          setButtonState("done");
          setTimeout(() => setButtonState("idle"), 4000);
        } else {
          setButtonState("error");
          setErrorMsg(s.last_error ?? `exit ${s.last_exit_code}`);
          setTimeout(() => setButtonState("idle"), 6000);
        }
      }
    }, 2000);
    return stopPolling;
  }, [buttonState, fetchStatus, stopPolling]);

  const handleClick = useCallback(async () => {
    if (buttonState === "running" || buttonState === "starting") return;
    setButtonState("starting");
    setErrorMsg("");

    try {
      const res = await fetch("/api/ingestor/run", {
        method: "POST",
        cache: "no-store",
      });
      if (res.status === 409) {
        setButtonState("running");
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setButtonState("running");
    } catch (err) {
      setButtonState("error");
      setErrorMsg(err instanceof Error ? err.message : "Unknown error");
      setTimeout(() => setButtonState("idle"), 6000);
    }
  }, [buttonState]);

  const isDisabled = buttonState === "starting" || buttonState === "running";

  return (
    <div className="flex items-center gap-2">
      <button
        disabled={isDisabled}
        onClick={handleClick}
        className={[
          "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors",
          buttonState === "done"
            ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
            : buttonState === "error"
              ? "bg-red-50 text-red-700 border border-red-200"
              : isDisabled
                ? "bg-zinc-100 text-zinc-400 border border-zinc-200 cursor-not-allowed"
                : "bg-zinc-900 text-white border border-zinc-900 hover:bg-zinc-700",
        ].join(" ")}
      >
        {buttonState === "starting" && (
          <span className="h-2 w-2 rounded-full bg-zinc-400 animate-pulse" />
        )}
        {buttonState === "running" && (
          <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
        )}
        {buttonState === "done" && (
          <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none">
            <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
        {buttonState === "error" && (
          <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none">
            <path d="M6 4v3M6 8.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        )}
        {buttonState === "idle" && "Run ingest"}
        {buttonState === "starting" && "Starting…"}
        {buttonState === "running" && "Running…"}
        {buttonState === "done" && "Done"}
        {buttonState === "error" && "Failed"}
      </button>

      {buttonState === "running" && status?.last_started_at && (
        <span className="font-mono text-[11px] text-zinc-400">
          started {formatRelative(status.last_started_at)}
        </span>
      )}
      {buttonState === "error" && errorMsg && (
        <span className="text-[11px] text-red-600 max-w-48 truncate" title={errorMsg}>
          {errorMsg}
        </span>
      )}
      {buttonState === "done" && status?.last_finished_at && (
        <span className="font-mono text-[11px] text-zinc-400">
          finished {formatRelative(status.last_finished_at)}
        </span>
      )}
    </div>
  );
}
