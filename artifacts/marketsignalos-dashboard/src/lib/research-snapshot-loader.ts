import {
  researchSnapshotSchema,
  type ResearchSnapshot,
} from "./research-snapshot";

export const scheduledSnapshotUrl =
  "https://raw.githubusercontent.com/kevinmwpi/marketsignalos/codex/research-snapshots/research-snapshot.json";
export const bundledSnapshotUrl = `${import.meta.env?.BASE_URL ?? "/"}data/research-snapshot.json`;
const MAX_BYTES = 2_000_000;

export type LoadedResearchSnapshot = {
  snapshot: ResearchSnapshot;
  url: string;
  source: "scheduled" | "bundled";
  degraded: boolean;
};

async function readSnapshot(
  url: string,
  parent: AbortSignal,
): Promise<ResearchSnapshot> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  parent.addEventListener("abort", abort, { once: true });
  if (parent.aborted) controller.abort();
  const timer = setTimeout(abort, 5_000);
  try {
    const response = await fetch(url, {
      cache: url === scheduledSnapshotUrl ? "no-cache" : "default",
      signal: controller.signal,
      credentials: "omit",
    });
    if (!response.ok || !response.body) throw new Error("Snapshot unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let bytes = 0;
    let text = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > MAX_BYTES) throw new Error("Snapshot exceeds size limit");
        text += decoder.decode(value, { stream: true });
      }
    } finally {
      await reader.cancel();
    }
    const snapshot = researchSnapshotSchema.parse(
      JSON.parse(text + decoder.decode()),
    );
    const generated = Date.parse(snapshot.generated_at);
    if (
      generated > Date.now() + 300_000 ||
      Date.parse(snapshot.started_at) > generated ||
      snapshot.trade_window.start > snapshot.trade_window.end
    ) {
      throw new Error("Invalid capture time");
    }
    return snapshot;
  } finally {
    clearTimeout(timer);
    parent.removeEventListener("abort", abort);
  }
}

export async function loadResearchSnapshot(
  signal: AbortSignal,
): Promise<LoadedResearchSnapshot> {
  const [scheduled, bundled] = await Promise.allSettled([
    readSnapshot(scheduledSnapshotUrl, signal),
    readSnapshot(bundledSnapshotUrl, signal),
  ]);
  if (signal.aborted) throw new Error("Snapshot load cancelled");
  if (
    scheduled.status === "fulfilled" &&
    (bundled.status !== "fulfilled" ||
      Date.parse(scheduled.value.generated_at) >=
        Date.parse(bundled.value.generated_at))
  ) {
    return {
      snapshot: scheduled.value,
      url: scheduledSnapshotUrl,
      source: "scheduled",
      degraded: false,
    };
  }
  if (bundled.status === "fulfilled") {
    return {
      snapshot: bundled.value,
      url: bundledSnapshotUrl,
      source: "bundled",
      degraded: true,
    };
  }
  throw new Error("No valid research capture is available");
}

// Preserve a newer capture already displayed if a later refresh only finds an older fallback.
export function retainNewest(
  current: LoadedResearchSnapshot | null,
  next: LoadedResearchSnapshot,
): LoadedResearchSnapshot {
  return current &&
    Date.parse(current.snapshot.generated_at) >
      Date.parse(next.snapshot.generated_at)
    ? { ...current, degraded: true }
    : next;
}
