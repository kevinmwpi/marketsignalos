import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveApiBase } from "./api-base";

test("a static build has no API, whatever else is configured", () => {
  assert.equal(resolveApiBase({ VITE_STATIC_SITE: "1" }), null);
  assert.equal(
    resolveApiBase({ VITE_STATIC_SITE: "1", VITE_API_BASE_URL: "https://api.example" }),
    null,
  );
});

test("a configured API base wins, without a trailing slash", () => {
  assert.equal(resolveApiBase({ VITE_API_BASE_URL: "https://api.example/" }), "https://api.example");
});

test("unset or empty falls back to same-origin /api", () => {
  assert.equal(resolveApiBase({}), "/api");
  assert.equal(resolveApiBase({ VITE_API_BASE_URL: "" }), "/api");
  assert.equal(resolveApiBase({ VITE_STATIC_SITE: "0" }), "/api");
});
