"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Progress = {
  stage: string;
  current?: number;
  total?: number;
  wallet?: string;
  detail?: string;
};

type IngestorStatus = {
  running: boolean;
  kind: IngestMode | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_exit_code: number | null;
  last_error: string | null;
  log_tail: string[];
  progress: Progress | null;
};

const STAGE_LABELS: Record<string, string> = {
  // Shallow + shared
  seed_watchlist: "Seeding watchlist",
  wallets: "Scanning wallet",
  markets: "Fetching Polymarket markets",
  enrichment: "Computing skill scores",
  kalshi_markets: "Fetching Kalshi markets",
  match_markets: "Matching Polymarket → Kalshi markets",
  // Deep-only
  deep_leaderboard: "Sweeping leaderboard matrix",
  refresh_review_state: "Refreshing wallet review-state",
  prune_review_state: "Pruning dormant wallets",
};

type IngestMode = "shallow" | "deep";

type ModeConfig = {
  endpoint: string;
  idleLabel: string;
};

const MODE_CONFIG: Record<IngestMode, ModeConfig> = {
  shallow: { endpoint: "/api/ingestor/run", idleLabel: "Run ingest" },
  deep: { endpoint: "/api/ingestor/run/deep", idleLabel: "Deep run" },
};

type ButtonState = "idle" | "starting" | "running" | "done" | "error";

type ErrorView =
  | { kind: "config"; missing: string[] }
  | { kind: "run"; message: string; logTail: string[] }
  | { kind: "transport"; message: string }
  | null;

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

function ProgressLabel({ progress }: { progress: Progress }) {
  const label = STAGE_LABELS[progress.stage] ?? progress.stage;
  const hasCount =
    typeof progress.current === "number" && typeof progress.total === "number";
  return (
    <span className="font-mono text-[11px] text-zinc-500">
      {label}
      {hasCount && (
        <>
          {" "}
          <span className="text-zinc-700 font-semibold">
            {progress.current}/{progress.total}
          </span>
        </>
      )}
      {progress.detail && !hasCount && (
        <span className="text-zinc-400"> ({progress.detail})</span>
      )}
    </span>
  );
}

// Stages that emit current/total counters worth visualizing as a bar.
const PROGRESS_BAR_STAGES = new Set(["wallets", "deep_leaderboard"]);

function ProgressBar({ progress }: { progress: Progress }) {
  if (
    !PROGRESS_BAR_STAGES.has(progress.stage) ||
    typeof progress.current !== "number" ||
    typeof progress.total !== "number" ||
    progress.total <= 0
  ) {
    return null;
  }
  const pct = Math.min(100, Math.max(0, (progress.current / progress.total) * 100));
  return (
    <div className="h-1 w-full max-w-md overflow-hidden rounded-full bg-zinc-100">
      <div
        className="h-full bg-amber-400 transition-[width] duration-500 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export default function IngestButton({ mode = "shallow" }: { mode?: IngestMode } = {}) {
  const config = MODE_CONFIG[mode];
  const [buttonState, setButtonState] = useState<ButtonState>("idle");
  const [status, setStatus] = useState<IngestorStatus | null>(null);
  const [errorView, setErrorView] = useState<ErrorView>(null);
  const [logExpanded, setLogExpanded] = useState(false);
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

  const dismissError = useCallback(() => {
    setErrorView(null);
    setLogExpanded(false);
    setButtonState("idle");
  }, []);

  useEffect(() => {
    fetchStatus().then((s) => {
      // Only resume the running display for *this* button's run kind. The
      // server shares one running flag across shallow + deep, so without the
      // kind check both buttons would mirror whichever run is in flight.
      if (s?.running && s.kind === mode) {
        setStatus(s);
        setButtonState("running");
      }
    });
    return stopPolling;
  }, [fetchStatus, stopPolling, mode]);

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
          setErrorView({
            kind: "run",
            message: s.last_error ?? `Exited with code ${s.last_exit_code ?? "?"}`,
            logTail: s.log_tail ?? [],
          });
        }
      }
    }, 2000);
    return stopPolling;
  }, [buttonState, fetchStatus, stopPolling]);

  const handleClick = useCallback(async () => {
    if (buttonState === "running" || buttonState === "starting") return;
    setButtonState("starting");
    setErrorView(null);
    setLogExpanded(false);

    let res: Response;
    try {
      res = await fetch(config.endpoint, {
        method: "POST",
        cache: "no-store",
      });
    } catch (err) {
      setButtonState("error");
      setErrorView({
        kind: "transport",
        message: err instanceof Error ? err.message : "Network error",
      });
      return;
    }

    if (res.status === 409) {
      // A run is already in flight. If it's our own kind, resume the running
      // display; otherwise the *other* button owns it — surface that rather
      // than mirroring a run this button didn't start.
      const s = await fetchStatus();
      if (s?.running && s.kind === mode) {
        setStatus(s);
        setButtonState("running");
      } else {
        setButtonState("error");
        setErrorView({
          kind: "transport",
          message:
            s?.kind === "deep"
              ? "A deep run is already in progress."
              : s?.kind === "shallow"
                ? "A shallow ingest is already in progress."
                : "Another ingest run is already in progress.",
        });
      }
      return;
    }

    let body: Record<string, unknown> = {};
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      // empty body
    }

    if (res.status === 400 && Array.isArray((body as { missing?: unknown }).missing)) {
      setButtonState("error");
      setErrorView({
        kind: "config",
        missing: ((body as { missing: unknown[] }).missing).map(String),
      });
      return;
    }

    if (!res.ok) {
      setButtonState("error");
      setErrorView({
        kind: "transport",
        message:
          (typeof body.detail === "string" ? body.detail : undefined) ??
          `HTTP ${res.status}`,
      });
      return;
    }

    setButtonState("running");
  }, [buttonState, config.endpoint, fetchStatus, mode]);

  const isDisabled = buttonState === "starting" || buttonState === "running";

  return (
    <div className="flex flex-col gap-2">
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
          {buttonState === "idle" && config.idleLabel}
          {buttonState === "starting" && "Starting…"}
          {buttonState === "running" && "Running…"}
          {buttonState === "done" && "Done"}
          {buttonState === "error" && "Failed"}
        </button>

        {buttonState === "running" && status?.progress && (
          <ProgressLabel progress={status.progress} />
        )}
        {buttonState === "running" && status?.last_started_at && (
          <span className="font-mono text-[11px] text-zinc-400">
            started {formatRelative(status.last_started_at)}
          </span>
        )}
        {buttonState === "done" && status?.last_finished_at && (
          <span className="font-mono text-[11px] text-zinc-400">
            finished {formatRelative(status.last_finished_at)}
          </span>
        )}
        {errorView && (
          <button
            type="button"
            onClick={dismissError}
            className="text-[11px] text-zinc-500 hover:text-zinc-700 underline underline-offset-2"
          >
            dismiss
          </button>
        )}
      </div>

      {buttonState === "running" && status?.progress && (
        <ProgressBar progress={status.progress} />
      )}

      {errorView?.kind === "config" && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-[11px] text-red-800 max-w-md">
          <div className="font-semibold mb-1">Ingestor is not configured.</div>
          <ul className="list-disc pl-4 space-y-1">
            {errorView.missing.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
          <div className="mt-2 text-red-600">
            Set these in your environment (e.g. <code>.env</code>) and restart the API.
          </div>
        </div>
      )}

      {errorView?.kind === "run" && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-[11px] text-red-800 max-w-md">
          <div className="font-semibold mb-1">Run failed</div>
          <div className="font-mono break-words">{errorView.message}</div>
          {errorView.logTail.length > 0 && (
            <div className="mt-2">
              <button
                type="button"
                onClick={() => setLogExpanded((v) => !v)}
                className="text-red-700 hover:text-red-900 underline underline-offset-2"
              >
                {logExpanded ? "hide" : "show"} log tail ({errorView.logTail.length} lines)
              </button>
              {logExpanded && (
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-red-100 p-2 font-mono text-[10px] text-red-900">
                  {errorView.logTail.join("\n")}
                </pre>
              )}
            </div>
          )}
        </div>
      )}

      {errorView?.kind === "transport" && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-[11px] text-red-800 max-w-md">
          <div className="font-semibold mb-1">Could not start ingest</div>
          <div className="font-mono break-words">{errorView.message}</div>
        </div>
      )}
    </div>
  );
}
