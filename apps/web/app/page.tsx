type TradeSignal = {
  source: string;
  market_ticker: string;
  trade_id: string;
  side: string;
  price: number;
  quantity: number;
  traded_at: string;
};

type SuspiciousAccountSignal = {
  account_id: string;
  account_first_seen_at: string;
  account_age_days: number;
  resolved_calls: number;
  wins: number;
  losses: number;
  win_rate: number;
  suspicion_score: number;
  last_activity_at: string;
};

async function getLatestTrades(apiBase: string): Promise<{ trades: TradeSignal[]; error?: string }> {
  try {
    const response = await fetch(`${apiBase}/signals/trades?limit=20`, {
      cache: "no-store",
    });

    if (!response.ok) {
      return { trades: [], error: `Trades API returned ${response.status}` };
    }

    const trades = (await response.json()) as TradeSignal[];
    return { trades };
  } catch {
    return { trades: [], error: "Unable to reach trades API" };
  }
}

async function getSuspiciousAccounts(
  apiBase: string,
): Promise<{ accounts: SuspiciousAccountSignal[]; error?: string }> {
  try {
    const response = await fetch(`${apiBase}/signals/suspicious-accounts?fresh_days=30&min_resolved=20&limit=20`, {
      cache: "no-store",
    });

    if (!response.ok) {
      return { accounts: [], error: `Suspicious accounts API returned ${response.status}` };
    }

    const accounts = (await response.json()) as SuspiciousAccountSignal[];
    return { accounts };
  } catch {
    return { accounts: [], error: "Unable to reach suspicious accounts API" };
  }
}

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

  const [{ trades, error: tradesError }, { accounts, error: suspiciousError }] = await Promise.all([
    getLatestTrades(apiBase),
    getSuspiciousAccounts(apiBase),
  ]);

  return (
    <div className="min-h-screen bg-zinc-50 px-6 py-10 text-zinc-900 dark:bg-black dark:text-zinc-100">
      <main className="mx-auto w-full max-w-5xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-3xl font-semibold">MarketSignalOS - Kalshi Signal Dashboard</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Data is served from our API storage layer, not directly from Kalshi on page refresh.
          </p>
        </header>

        {tradesError ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {tradesError}. Start the API and ingestor to populate this table.
          </div>
        ) : null}

        {suspiciousError ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {suspiciousError}. Add fills plus settled markets ingestion to populate suspicious-account scoring.
          </div>
        ) : null}

        <section className="overflow-x-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
          <div className="border-b border-zinc-200 px-3 py-2 text-sm font-medium dark:border-zinc-800">Latest Kalshi Trades</div>
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-zinc-200 bg-zinc-100 text-xs uppercase tracking-wide text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
              <tr>
                <th className="px-3 py-2">time</th>
                <th className="px-3 py-2">market</th>
                <th className="px-3 py-2">side</th>
                <th className="px-3 py-2">price</th>
                <th className="px-3 py-2">qty</th>
                <th className="px-3 py-2">trade id</th>
              </tr>
            </thead>
            <tbody>
              {trades.length === 0 ? (
                <tr>
                  <td className="px-3 py-4 text-zinc-500 dark:text-zinc-400" colSpan={6}>
                    No ingested trades found yet.
                  </td>
                </tr>
              ) : (
                trades.map((trade) => (
                  <tr key={`${trade.source}-${trade.trade_id}`} className="border-b border-zinc-100 dark:border-zinc-900">
                    <td className="px-3 py-2">{trade.traded_at}</td>
                    <td className="px-3 py-2">{trade.market_ticker}</td>
                    <td className="px-3 py-2 uppercase">{trade.side}</td>
                    <td className="px-3 py-2">{trade.price}</td>
                    <td className="px-3 py-2">{trade.quantity}</td>
                    <td className="px-3 py-2">{trade.trade_id}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>

        <section className="overflow-x-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
          <div className="border-b border-zinc-200 px-3 py-2 text-sm font-medium dark:border-zinc-800">
            Suspiciously Accurate Fresh Accounts (30d, min 20 resolved)
          </div>
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-zinc-200 bg-zinc-100 text-xs uppercase tracking-wide text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
              <tr>
                <th className="px-3 py-2">account</th>
                <th className="px-3 py-2">age days</th>
                <th className="px-3 py-2">resolved</th>
                <th className="px-3 py-2">wins</th>
                <th className="px-3 py-2">losses</th>
                <th className="px-3 py-2">win rate</th>
                <th className="px-3 py-2">score</th>
                <th className="px-3 py-2">last activity</th>
              </tr>
            </thead>
            <tbody>
              {accounts.length === 0 ? (
                <tr>
                  <td className="px-3 py-4 text-zinc-500 dark:text-zinc-400" colSpan={8}>
                    No suspicious fresh accounts yet. This can mean no ingested fills/resolutions or no qualifying accounts.
                  </td>
                </tr>
              ) : (
                accounts.map((account) => (
                  <tr key={account.account_id} className="border-b border-zinc-100 dark:border-zinc-900">
                    <td className="px-3 py-2">{account.account_id}</td>
                    <td className="px-3 py-2">{account.account_age_days}</td>
                    <td className="px-3 py-2">{account.resolved_calls}</td>
                    <td className="px-3 py-2">{account.wins}</td>
                    <td className="px-3 py-2">{account.losses}</td>
                    <td className="px-3 py-2">{(account.win_rate * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{account.suspicion_score.toFixed(4)}</td>
                    <td className="px-3 py-2">{account.last_activity_at}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      </main>
    </div>
  );
}
