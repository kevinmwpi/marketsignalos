type ApiEnv = { VITE_STATIC_SITE?: string; VITE_API_BASE_URL?: string };

/**
 * Where the signal API lives, or null when the build has no API behind it.
 *
 * The public site is a static build (GitHub Pages) that only reads the research
 * snapshot, so it sets VITE_STATIC_SITE=1 and makes no API requests at all
 * rather than showing failed-request errors. Other builds use VITE_API_BASE_URL
 * when it is set and non-empty, else same-origin /api.
 */
export function resolveApiBase(env: ApiEnv): string | null {
  if (env.VITE_STATIC_SITE === "1") return null;
  return env.VITE_API_BASE_URL?.replace(/\/$/, "") || "/api";
}

export const apiBase = resolveApiBase((import.meta.env ?? {}) as ApiEnv);
