export const dynamic = "force-dynamic";

import IngestButton from "./components/IngestButton";
import PolymarketLeaderboardPanel, { type PolymarketWalletSkill } from "./components/PolymarketLeaderboardPanel";
import SkilledBetsPanel, { type SkilledBet } from "./components/SkilledBetsPanel";

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
  skilledBets: ApiResult<SkilledBet[]>;
}> {
  const [polymarketLeaderboard, skilledBets] = await Promise.all([
    fetchJson<PolymarketWalletSkill[]>(
      apiUrl(apiBase, "/signals/polymarket-leaderboard?min_resolved=5&limit=10"),
      "polymarket leaderboard API",
    ),
    fetchJson<SkilledBet[]>(
      apiUrl(apiBase, "/signals/skilled-bets?min_skill=0.8&min_resolved=20&limit=50"),
      "skilled-bets API",
    ),
  ]);

  return { polymarketLeaderboard, skilledBets };
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

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
  const { polymarketLeaderboard, skilledBets } = await getDashboardData(apiBase);
  const polymarketLeaderboardRows = polymarketLeaderboard.data ?? [];
  const bets = skilledBets.data ?? [];
  const errors = [polymarketLeaderboard.error, skilledBets.error].filter(
    (error): error is string => Boolean(error),
  );
  const isLive = errors.length === 0;
  const uniqueWallets = new Set(bets.map((b) => b.proxy_wallet)).size;
  const capitalHeld = bets.reduce((acc, b) => acc + b.current_position_value_usdc, 0);
  const tailableOnKalshi = bets.filter((b) => b.kalshi_ticker && b.kalshi_market_url).length;

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <nav className="sticky top-0 z-10 border-b border-zinc-200 bg-white/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
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
            Skilled wallet bets
          </h1>
          <p className="mt-1.5 max-w-2xl text-sm text-zinc-500">
            Recent BUY entries from Polymarket wallets whose on-chain win rate beats
            random at <span className="font-mono">≥80%</span> confidence over{" "}
            <span className="font-mono">≥20</span> resolved bets — and that are
            still holding the position. Each row links to the Polymarket event and,
            where an equivalent Kalshi market exists, a direct deep link you can
            use to tail the bet on Kalshi.
          </p>
        </div>

        <ErrorBanner errors={errors} />

        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Active bets
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">
              {bets.length}
            </p>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Unique wallets
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">
              {uniqueWallets}
            </p>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Capital held
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">
              {usdFormatter.format(capitalHeld)}
            </p>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Tailable on Kalshi
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">
              {tailableOnKalshi}
              <span className="ml-1 text-sm text-zinc-400">/ {bets.length}</span>
            </p>
          </div>
        </div>

        <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
          <section>
            <SkilledBetsPanel bets={bets} />
          </section>

          <aside className="space-y-4">
            <PolymarketLeaderboardPanel rows={polymarketLeaderboardRows} />
          </aside>
        </div>
      </main>
    </div>
  );
}
