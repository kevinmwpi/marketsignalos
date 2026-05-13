export const dynamic = "force-dynamic";

import Link from "next/link";

import SkilledBetsPanel, { type SkilledBet } from "../components/SkilledBetsPanel";

async function fetchSkilledBets(apiBase: string): Promise<{
  data: SkilledBet[] | null;
  error?: string;
}> {
  const url = new URL("/signals/skilled-bets", apiBase);
  url.searchParams.set("min_skill", "0.8");
  url.searchParams.set("min_resolved", "20");
  url.searchParams.set("limit", "50");
  try {
    const resp = await fetch(url.toString(), { cache: "no-store" });
    if (!resp.ok) {
      return { data: null, error: `API ${resp.status}` };
    }
    return { data: (await resp.json()) as SkilledBet[] };
  } catch (err) {
    return { data: null, error: err instanceof Error ? err.message : String(err) };
  }
}

export default async function SkilledBetsPage(): Promise<React.ReactElement> {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
  const { data, error } = await fetchSkilledBets(apiBase);
  const bets = data ?? [];
  const apiLive = !error;

  return (
    <div className="min-h-screen bg-zinc-50">
      <nav className="sticky top-0 z-10 border-b border-zinc-200 bg-white/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-6">
            <Link className="flex items-center gap-2.5" href="/">
              <svg className="h-4 w-4 text-zinc-900" fill="none" viewBox="0 0 16 16">
                <rect fill="currentColor" height="7" rx="1" width="7" />
                <rect fill="currentColor" height="7" opacity="0.5" rx="1" width="7" x="9" />
                <rect fill="currentColor" height="7" opacity="0.5" rx="1" width="7" y="9" />
                <rect fill="currentColor" height="7" rx="1" width="7" x="9" y="9" />
              </svg>
              <span className="text-sm font-semibold tracking-tight text-zinc-900">
                MarketSignalOS
              </span>
            </Link>
            <div className="flex items-center gap-4 text-xs">
              <Link className="text-zinc-500 hover:text-zinc-900" href="/">
                Dashboard
              </Link>
              <span className="font-semibold text-zinc-900">Skilled bets</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`h-1.5 w-1.5 rounded-full ${apiLive ? "bg-emerald-500" : "bg-amber-400"}`}
            />
            <span className="text-xs text-zinc-500">
              {apiLive ? "API connected" : `API degraded${error ? ` (${error})` : ""}`}
            </span>
          </div>
        </div>
      </nav>

      <main className="mx-auto max-w-7xl px-6 pb-16 pt-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
            Skilled wallet bets
          </h1>
          <p className="mt-1.5 max-w-2xl text-sm text-zinc-500">
            Recent BUY entries from Polymarket wallets whose on-chain win rate beats
            random at <span className="font-mono">≥80%</span> confidence over{" "}
            <span className="font-mono">≥20</span> resolved bets — and that are
            still holding the position. Sorted newest first. Each row links to the
            Polymarket event and the wallet&apos;s profile.
          </p>
        </div>

        {/* Stat strip */}
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
              {new Set(bets.map((b) => b.proxy_wallet)).size}
            </p>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Capital held
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">
              {new Intl.NumberFormat("en-US", {
                style: "currency",
                currency: "USD",
                notation: "compact",
                maximumFractionDigits: 1,
              }).format(
                bets.reduce((acc, b) => acc + b.current_position_value_usdc, 0),
              )}
            </p>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              Min skill
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold text-zinc-900">80%</p>
          </div>
        </div>

        <SkilledBetsPanel bets={bets} />
      </main>
    </div>
  );
}
