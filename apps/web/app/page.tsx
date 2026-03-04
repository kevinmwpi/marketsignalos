type SkilledAccountSignal = {
  account_id: string;
  account_first_seen_at: string;
  account_age_days: number;
  resolved_calls: number;
  wins: number;
  losses: number;
  expected_wins: number;
  excess_wins: number;
  win_rate: number;
  stddevs_above_expected: number;
  skill_score: number;
  insider_like_score: number;
  anomaly_probability: number;
  last_activity_at: string;
};

async function getLeaderboard(
  apiBase: string,
): Promise<{ accounts: SkilledAccountSignal[]; error?: string }> {
  try {
    const response = await fetch(
      `${apiBase}/signals/leaderboard?fresh_days=30&min_resolved=20&limit=25`,
      { cache: "no-store" },
    );

    if (!response.ok) {
      return { accounts: [], error: `Leaderboard API returned ${response.status}` };
    }

    const accounts = (await response.json()) as SkilledAccountSignal[];
    return { accounts };
  } catch {
    return { accounts: [], error: "Unable to reach leaderboard API" };
  }
}

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  const { accounts, error } = await getLeaderboard(apiBase);

  return (
    <div className="min-h-screen bg-slate-950 px-4 py-10 text-slate-100 sm:px-8">
      <main className="mx-auto w-full max-w-6xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">MarketSignalOS Skill Leaderboard</h1>
          <p className="text-sm text-slate-300">
            Ranked by statistical outperformance and insider-like behavior signals from ingested account data.
          </p>
        </header>

        {error ? (
          <div className="rounded-md border border-amber-600 bg-amber-950/50 p-4 text-sm text-amber-100">
            {error}. Run API plus ingestion and refresh.
          </div>
        ) : null}

        <section className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-900/80">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-slate-700 bg-slate-900 text-xs uppercase tracking-wide text-slate-300">
              <tr>
                <th className="px-3 py-2">rank</th>
                <th className="px-3 py-2">account</th>
                <th className="px-3 py-2">insider-like</th>
                <th className="px-3 py-2">skill</th>
                <th className="px-3 py-2">anomaly</th>
                <th className="px-3 py-2">z-score</th>
                <th className="px-3 py-2">resolved</th>
                <th className="px-3 py-2">win rate</th>
                <th className="px-3 py-2">last activity</th>
              </tr>
            </thead>
            <tbody>
              {accounts.length === 0 ? (
                <tr>
                  <td className="px-3 py-6 text-slate-400" colSpan={9}>
                    No accounts qualify yet for this leaderboard.
                  </td>
                </tr>
              ) : (
                accounts.map((account, index) => (
                  <tr key={account.account_id} className="border-b border-slate-800">
                    <td className="px-3 py-2">{index + 1}</td>
                    <td className="px-3 py-2">{account.account_id}</td>
                    <td className="px-3 py-2">{(account.insider_like_score * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{(account.skill_score * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{(account.anomaly_probability * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{account.stddevs_above_expected.toFixed(2)}</td>
                    <td className="px-3 py-2">{account.resolved_calls}</td>
                    <td className="px-3 py-2">{(account.win_rate * 100).toFixed(1)}%</td>
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
