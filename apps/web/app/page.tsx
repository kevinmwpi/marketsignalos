type TradeSignal = {
  source: string;
  market_ticker: string;
  trade_id: string;
  side: string;
  price: number;
  quantity: number;
  traded_at: string;
};

async function getLatestTrades(): Promise<{ trades: TradeSignal[]; error?: string }> {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

  try {
    const response = await fetch(`${apiBase}/signals/trades?limit=20`, {
      cache: "no-store",
    });

    if (!response.ok) {
      return { trades: [], error: `API returned ${response.status}` };
    }

    const trades = (await response.json()) as TradeSignal[];
    return { trades };
  } catch {
    return { trades: [], error: "Unable to reach API" };
  }
}

export default async function Home() {
  const { trades, error } = await getLatestTrades();

  return (
    <div className="min-h-screen bg-zinc-50 px-6 py-10 text-zinc-900 dark:bg-black dark:text-zinc-100">
      <main className="mx-auto w-full max-w-5xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-3xl font-semibold">MarketSignalOS — Latest Kalshi Trades</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Data is served from our API storage layer, not directly from Kalshi on page refresh.
          </p>
        </header>

        {error ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {error}. Start the API and ingestor to populate this table.
          </div>
        ) : null}

        <section className="overflow-x-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
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
      </main>
    </div>
  );
}
