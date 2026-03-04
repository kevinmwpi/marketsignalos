type LeaderboardRow = {
  account_id: string;
  account_first_seen_at: string;
  account_age_days: number;
  resolved_calls: number;
  wins: number;
  losses: number;
  win_rate: number;
  expected_wins: number;
  excess_wins: number;
  stddevs_above_expected: number;
  skill_likelihood: number;
  insider_like_score: number;
  anomaly_probability: number;
  last_activity_at: string;
};

async function getLeaderboard(
  apiBase: string,
): Promise<{ rows: LeaderboardRow[]; error?: string }> {
  try {
    const response = await fetch(
      `${apiBase}/signals/leaderboard?fresh_days=30&min_resolved=20&limit=25`,
      { cache: "no-store" },
    );
    if (!response.ok) {
      return { rows: [], error: `Leaderboard API returned ${response.status}` };
    }
    const rows = (await response.json()) as LeaderboardRow[];
    return { rows };
  } catch {
    return { rows: [], error: "Unable to reach leaderboard API" };
  }
}

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  const { rows, error } = await getLeaderboard(apiBase);

  return (
    <div className="min-h-screen bg-zinc-50 px-6 py-10 text-zinc-900 dark:bg-black dark:text-zinc-100">
      <main className="mx-auto w-full max-w-6xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-3xl font-semibold">MarketSignalOS Skill Leaderboard</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Ranked by likelihood of skill versus luck, plus insider-like enrichment signals.
          </p>
        </header>

        {error ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {error}
          </div>
        ) : null}

        <section className="overflow-x-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-zinc-200 bg-zinc-100 text-xs uppercase tracking-wide text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
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
              {rows.length === 0 ? (
                <tr>
                  <td className="px-3 py-4 text-zinc-500 dark:text-zinc-400" colSpan={9}>
                    No qualifying accounts yet.
                  </td>
                </tr>
              ) : (
                rows.map((row, index) => (
                  <tr key={row.account_id} className="border-b border-zinc-100 dark:border-zinc-900">
                    <td className="px-3 py-2">{index + 1}</td>
                    <td className="px-3 py-2">{row.account_id}</td>
                    <td className="px-3 py-2">{(row.insider_like_score * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{(row.skill_likelihood * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{(row.anomaly_probability * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{row.stddevs_above_expected.toFixed(2)}</td>
                    <td className="px-3 py-2">{row.resolved_calls}</td>
                    <td className="px-3 py-2">{(row.win_rate * 100).toFixed(1)}%</td>
                    <td className="px-3 py-2">{row.last_activity_at}</td>
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

