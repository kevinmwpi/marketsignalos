export const dynamic = "force-dynamic";

import Link from "next/link";

import CrossExchangePanel, { type CrossExchangeSignal } from "./components/CrossExchangePanel";
import IngestButton from "./components/IngestButton";
import PolymarketLeaderboardPanel, { type PolymarketWalletSkill } from "./components/PolymarketLeaderboardPanel";

type ApiResult<T> = {
  data: T | null;
  error?: string;
};

function apiUrl(apiBase: string, path: string): string {
  return `${apiBase.replace(/\/$/, "")}${path}`;
}

async function fetchJson<T>(url: string, label: string): Promise<ApiResult<T>> {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      return { data: null, error: `${label} returned ${response.status}` };
    }
    return { data: (await response.json()) as T };
  } catch {
    return { data: null, error: `Unable to reach ${label}` };
  }
}

async function getDashboardData(apiBase: string): Promise<{
  polymarketLeaderboard: ApiResult<PolymarketWalletSkill[]>;
  crossExchange: ApiResult<CrossExchangeSignal[]>;
}> {
  const [polymarketLeaderboard, crossExchange] = await Promise.all([
    fetchJson<PolymarketWalletSkill[]>(
      apiUrl(apiBase, "/signals/polymarket-leaderboard?min_resolved=5&limit=10"),
      "polymarket leaderboard API",
    ),
    fetchJson<CrossExchangeSignal[]>(
      apiUrl(
        apiBase,
        "/signals/cross-exchange?min_skill=0.6&min_resolved=5&min_dislocation_pct=2&limit=15",
      ),
      "cross-exchange API",
    ),
  ]);

  return { polymarketLeaderboard, crossExchange };
}

function ErrorBanner({ errors }: { errors: string[] }) {
  if (errors.length === 0) return null;
  return (
    <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
      <span className="text-sm font-semibold text-amber-800">Signal feeds degraded: </span>
      <span className="text-sm text-amber-700">{errors.join(" · ")}</span>
    </div>
  );
}

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
  const { polymarketLeaderboard, crossExchange } = await getDashboardData(apiBase);
  const polymarketLeaderboardRows = polymarketLeaderboard.data ?? [];
  const crossExchangeSignals = crossExchange.data ?? [];
  const crossExchangeGeneratedAt =
    crossExchangeSignals.length > 0 ? crossExchangeSignals[0].observed_at : null;
  const errors = [polymarketLeaderboard.error, crossExchange.error].filter(
    (error): error is string => Boolean(error),
  );
  const isLive = errors.length === 0;

  return (
    <div className="min-h-screen bg-[#fafafa]">
      {/* Top nav */}
      <nav className="sticky top-0 z-10 border-b border-zinc-200 bg-white/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2.5">
              <svg className="h-4 w-4 text-zinc-900" fill="none" viewBox="0 0 16 16">
                <rect fill="currentColor" height="7" rx="1" width="7" />
                <rect fill="currentColor" height="7" opacity="0.5" rx="1" width="7" x="9" />
                <rect fill="currentColor" height="7" opacity="0.5" rx="1" width="7" y="9" />
                <rect fill="currentColor" height="7" rx="1" width="7" x="9" y="9" />
              </svg>
              <span className="text-sm font-semibold tracking-tight text-zinc-900">
                MarketSignalOS
              </span>
            </div>
            <div className="flex items-center gap-4 text-xs">
              <span className="font-semibold text-zinc-900">Dashboard</span>
              <Link className="text-zinc-500 hover:text-zinc-900" href="/skilled-bets">
                Skilled bets
              </Link>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <IngestButton />
            <div className="flex items-center gap-2">
              <span
                className={`h-1.5 w-1.5 rounded-full ${isLive ? "bg-emerald-500" : "bg-amber-400"}`}
              />
              <span className="text-xs text-zinc-500">{isLive ? "API connected" : "API degraded"}</span>
            </div>
          </div>
        </div>
      </nav>

      <main className="mx-auto max-w-7xl px-6 pb-16 pt-8">
        <div className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
            Cross-exchange signal
          </h1>
          <p className="mt-1.5 max-w-2xl text-sm text-zinc-500">
            Tradeable price dislocations between Polymarket and Kalshi, weighted
            by the skill of the Polymarket wallets holding each position.{" "}
            <Link className="font-medium text-zinc-700 underline" href="/skilled-bets">
              See the skilled-bets feed →
            </Link>
          </p>
        </div>

        <ErrorBanner errors={errors} />

        <div className="mb-8">
          <CrossExchangePanel
            generatedAt={crossExchangeGeneratedAt}
            signals={crossExchangeSignals}
          />
        </div>

        <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
          <section>
            <div className="mb-4 flex items-baseline justify-between">
              <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                How to use
              </h2>
            </div>
            <div className="rounded-lg border border-zinc-200 bg-white p-5 text-sm leading-relaxed text-zinc-600">
              <ol className="ml-5 list-decimal space-y-2">
                <li>
                  Click <span className="font-mono text-zinc-900">Run ingest</span> to
                  scan Polymarket leaderboards (across day, week, month, and all-time
                  windows), pull each top wallet&apos;s activity, score them, and
                  match their currently-held positions against Kalshi&apos;s public
                  market list.
                </li>
                <li>
                  Browse{" "}
                  <Link className="font-medium text-zinc-900 underline" href="/skilled-bets">
                    /skilled-bets
                  </Link>{" "}
                  to see what high-skill wallets are actively holding — each row
                  carries a direct Polymarket link and, where a match exists, a
                  Kalshi mirror you can tail.
                </li>
                <li>
                  Use the panel above for the price-dislocation view: same wallet
                  positions, but framed as &quot;buy YES on one exchange, sell on the
                  other&quot;.
                </li>
              </ol>
            </div>
          </section>

          <aside className="space-y-4">
            <PolymarketLeaderboardPanel rows={polymarketLeaderboardRows} />
          </aside>
        </div>
      </main>
    </div>
  );
}
