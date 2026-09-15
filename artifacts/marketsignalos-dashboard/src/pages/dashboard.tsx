import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import ExitSignalsPanel, { type ExitSignal } from "@/components/ExitSignalsPanel";
import IngestButton from "@/components/IngestButton";
import PolymarketLeaderboardPanel, {
  type PolymarketWalletSkill,
} from "@/components/PolymarketLeaderboardPanel";
import SkilledBetsPanel, { type SkilledBet } from "@/components/SkilledBetsPanel";
import WatchlistForm from "@/components/WatchlistForm";

type DashboardPayload = {
  skilled_bets?: SkilledBet[];
  bets?: SkilledBet[];
  exit_signals?: ExitSignal[];
  exits?: ExitSignal[];
  leaderboard?: PolymarketWalletSkill[];
  polymarket_wallets?: PolymarketWalletSkill[];
  generated_at?: string;
  as_of?: string;
};

const configuredSignalBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
const signalUrl = (path: string) => `${configuredSignalBase || "/api"}${path}`;

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(signalUrl(path), { cache: "no-store" });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function arrayAt<T>(value: unknown, keys: string[]): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  for (const key of keys) {
    const candidate = (value as Record<string, unknown>)[key];
    if (Array.isArray(candidate)) return candidate as T[];
  }
  return [];
}

function DashboardSkeleton() {
  return (
    <div className="space-y-3" aria-label="Loading market signals">
      {[1, 2, 3].map((row) => (
        <div className="h-36 animate-pulse rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]" key={row} />
      ))}
    </div>
  );
}

function Header() {
  return (
    <header className="border-b border-[hsl(var(--border))] bg-[hsl(var(--card))]">
      <div className="mx-auto flex max-w-[1520px] items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <Link href="/" className="flex items-center gap-3" data-testid="link-home">
          <span className="grid h-8 w-8 place-items-center rounded-md bg-[hsl(var(--primary))] font-mono text-sm font-bold text-[hsl(var(--primary-foreground))]">
            MS
          </span>
          <span>
            <span className="block text-sm font-bold tracking-tight text-[hsl(var(--foreground))]">MarketSignalOS</span>
            <span className="block font-mono text-[9px] uppercase tracking-[0.2em] text-[hsl(var(--muted-foreground))]">signal desk / live</span>
          </span>
        </Link>
        <div className="flex items-center gap-3">
          <span className="hidden font-mono text-[10px] uppercase tracking-widest text-[hsl(var(--muted-foreground))] sm:inline">Polymarket research terminal</span>
          <span className="flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-emerald-700">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-600" /> online
          </span>
        </div>
      </div>
    </header>
  );
}

export default function DashboardPage() {
  const [payload, setPayload] = useState<DashboardPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setRefreshing(true);
    setError(null);
    try {
      const [bets, exits, leaderboard, summary] = await Promise.all([
        getJson("/signals/skilled-bets?min_skill=0.8&min_resolved=20&min_independent_events=20&max_bet_age_days=90&require_positive_edge=true&limit=50"),
        getJson("/signals/exits?limit=10"),
        getJson("/signals/polymarket-leaderboard?min_resolved=20&min_skill=0.8&tailability=tailable&limit=10"),
        getJson("/signals/skilled-bets/summary"),
      ]);
      const raw: unknown = { bets, exits, leaderboard, summary };
      const root = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
      const nested = root.data && typeof root.data === "object" ? root.data : root;
      const source = nested as DashboardPayload;
      setPayload({
        ...source,
        skilled_bets: arrayAt<SkilledBet>(nested, ["skilled_bets", "bets", "actionable_bets"]),
        exit_signals: arrayAt<ExitSignal>(nested, ["exit_signals", "exits"]),
        leaderboard: arrayAt<PolymarketWalletSkill>(nested, ["leaderboard", "polymarket_wallets", "wallets"]),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signal API is unavailable");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const bets = useMemo(() => payload?.skilled_bets ?? [], [payload]);
  const exits = useMemo(() => payload?.exit_signals ?? [], [payload]);
  const leaderboard = useMemo(() => payload?.leaderboard ?? [], [payload]);
  const asOf = payload?.generated_at ?? payload?.as_of;

  return (
    <div className="min-h-[100dvh] bg-[hsl(var(--background))] text-[hsl(var(--foreground))]">
      <Header />
      <main className="mx-auto max-w-[1520px] space-y-6 px-4 pb-16 pt-5 sm:px-6">
        <div className="flex flex-col justify-between gap-4 border-b border-[hsl(var(--border))] pb-5 lg:flex-row lg:items-end">
          <div>
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.22em] text-[hsl(var(--muted-foreground))]">Research dashboard / 01</p>
            <h1 className="mt-1 text-2xl font-bold tracking-tight sm:text-3xl">Actionable skilled bets</h1>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">
              Wallets with demonstrated edge, still-open positions, and a tradeable path to follow. Prices and qualification state are refreshed by the ingestor.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {asOf && <span className="font-mono text-[10px] text-[hsl(var(--muted-foreground))]">updated {new Date(asOf).toLocaleString()}</span>}
            <button
              className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-3 py-1.5 font-mono text-[11px] font-semibold text-[hsl(var(--foreground))] transition-colors hover:bg-[hsl(var(--secondary))] disabled:opacity-50"
              data-testid="button-refresh-signals"
              disabled={refreshing}
              onClick={() => void load()}
              type="button"
            >
              {refreshing ? "Refreshing…" : "Refresh signals"}
            </button>
          </div>
        </div>

        <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]" aria-label="Signal feed">
          <div className="min-w-0">
            {loading ? <DashboardSkeleton /> : error ? (
              <div className="rounded-lg border border-red-200 bg-red-50 p-6" data-testid="status-dashboard-error">
                <p className="text-sm font-semibold text-red-800">Could not load signal feed</p>
                <p className="mt-1 font-mono text-xs text-red-700">{error}</p>
                <button className="mt-4 rounded-md bg-red-800 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-900" data-testid="button-retry-signals" onClick={() => void load()} type="button">Retry</button>
              </div>
            ) : (
              <SkilledBetsPanel bets={bets} />
            )}
          </div>
          <aside className="space-y-4">
            <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">Ingestion controls</p>
                  <p className="mt-1 text-sm font-semibold">Refresh the research layer</p>
                </div>
                <span className="font-mono text-[10px] text-[hsl(var(--muted-foreground))]">API / ingestor</span>
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <IngestButton mode="shallow" />
                <IngestButton mode="deep" />
              </div>
              <p className="mt-3 text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))]">Shallow keeps prices and positions current. Deep rebuilds the leaderboard matrix and review state.</p>
            </div>
            <ExitSignalsPanel exits={exits} />
            <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4">
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">Wallet watchlist</p>
              <p className="mt-1 text-sm font-semibold">Pin a wallet for the next run</p>
              <p className="mt-1 mb-3 text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))]">Addresses are hydrated and scored on the next ingest.</p>
              <WatchlistForm />
            </div>
          </aside>
        </section>

        <section className="space-y-3 pt-2" aria-label="Leaderboard">
          <div className="flex items-end justify-between gap-3">
            <div>
              <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">Leaderboard / posterior evidence</p>
              <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">Research candidates ranked by forecast skill and conservative edge.</p>
            </div>
            <Link href="/" className="font-mono text-[10px] font-semibold uppercase tracking-wider text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]" data-testid="link-leaderboard-top">top of desk</Link>
          </div>
          {loading ? <div className="h-48 animate-pulse rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]" /> : <PolymarketLeaderboardPanel rows={leaderboard} />}
        </section>
      </main>
    </div>
  );
}