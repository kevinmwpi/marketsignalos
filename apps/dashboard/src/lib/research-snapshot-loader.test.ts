import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  loadResearchSnapshot,
  retainNewest,
  scheduledSnapshotUrl,
} from "./research-snapshot-loader";

const base = JSON.parse(
  readFileSync(
    new URL("../../public/data/research-snapshot.json", import.meta.url),
    "utf8",
  ),
);
const stamp = (hoursAgo: number) =>
  new Date(Date.now() - hoursAgo * 3_600_000).toISOString();
const capture = (hoursAgo: number) => ({
  ...base,
  started_at: stamp(hoursAgo + 1),
  generated_at: stamp(hoursAgo),
});

test("scheduled captures load independently of the API and bundled file", async (t) => {
  t.mock.method(globalThis, "fetch", async (url: string) => {
    assert.ok(!url.includes("/api/"));
    return url === scheduledSnapshotUrl
      ? new Response(JSON.stringify(capture(1)))
      : new Response("Missing", { status: 404 });
  });
  const result = await loadResearchSnapshot(new AbortController().signal);
  assert.equal(result.source, "scheduled");
  assert.equal(result.degraded, false);
});

test("failed, malformed, future, and oversized remote captures fall back honestly", async (t) => {
  for (const response of [
    new Response("Unavailable", { status: 503 }),
    new Response("{}"),
    new Response(JSON.stringify(capture(-1))),
    new Response("x".repeat(2_000_001)),
  ]) {
    const mock = t.mock.method(globalThis, "fetch", async (url: string) =>
      url === scheduledSnapshotUrl
        ? response
        : new Response(JSON.stringify(capture(2))),
    );
    const result = await loadResearchSnapshot(new AbortController().signal);
    assert.equal(result.source, "bundled");
    assert.equal(result.degraded, true);
    mock.mock.restore();
  }
});

test("newest valid capture wins; a refresh cannot regress displayed data", async (t) => {
  t.mock.method(
    globalThis,
    "fetch",
    async (url: string) =>
      new Response(
        JSON.stringify(capture(url === scheduledSnapshotUrl ? 3 : 2)),
      ),
  );
  const result = await loadResearchSnapshot(new AbortController().signal);
  assert.equal(result.source, "bundled");
  const current = {
    ...result,
    snapshot: capture(1),
    source: "scheduled" as const,
    degraded: false,
  };
  const retained = retainNewest(current, result);
  assert.equal(retained.snapshot, current.snapshot);
  assert.equal(retained.degraded, true);
});

test("unavailable sources and cancelled requests do not invent an empty success", async (t) => {
  t.mock.method(
    globalThis,
    "fetch",
    async () => new Response("Unavailable", { status: 503 }),
  );
  await assert.rejects(loadResearchSnapshot(new AbortController().signal));
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(loadResearchSnapshot(controller.signal), /cancelled/);
});
