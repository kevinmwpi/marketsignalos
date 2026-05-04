export const dynamic = "force-dynamic";

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

type OrderflowAnomaly = {
  source: string;
  market_ticker: string;
  trade_id: string;
  traded_at: string;
  side: string;
  price: number;
  quantity: number;
  anomaly_type: string;
  anomaly_score: number;
  details: string;
};

type OpportunityDriver = {
  label: string;
  value: string;
  contribution: number;
  tone: "positive" | "neutral" | "risk";
};

type OpportunitySignal = {
  signal_id: string;
  signal_type: "skilled_account" | "orderflow_dislocation";
  source: string;
  entity: string;
  market_ticker: string | null;
  direction: string | null;
  title: string;
  thesis: string;
  probability: number;
  opportunity_score: number;
  confidence: number;
  expected_value_basis: string;
  observed_at: string;
  drivers: OpportunityDriver[];
  next_step: string;
};

type OpportunityDashboard = {
  generated_at: string;
  summary: {
    candidate_count: number;
    skilled_account_count: number;
    orderflow_event_count: number;
    top_probability: number;
    top_opportunity_score: number;
    freshness_window_days: number;
  };
  opportunities: OpportunitySignal[];
};

type ApiResult<T> = {
  data: T | null;
  error?: string;
};

const numberFormatter = new Intl.NumberFormat("en-US");
const compactFormatter = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

const emptySummary: OpportunityDashboard["summary"] = {
  candidate_count: 0,
  skilled_account_count: 0,
  orderflow_event_count: 0,
  top_probability: 0,
  top_opportunity_score: 0,
  freshness_window_days: 30,
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
  dashboard: ApiResult<OpportunityDashboard>;
  leaderboard: ApiResult<LeaderboardRow[]>;
  orderflow: ApiResult<OrderflowAnomaly[]>;
}> {
  const [dashboard, leaderboard, orderflow] = await Promise.all([
    fetchJson<OpportunityDashboard>(
      apiUrl(apiBase, "/signals/opportunities?fresh_days=30&min_resolved=20&limit=10"),
      "opportunity API",
    ),
    fetchJson<LeaderboardRow[]>(
      apiUrl(apiBase, "/signals/leaderboard?fresh_days=30&min_resolved=20&limit=8"),
      "leaderboard API",
    ),
    fetchJson<OrderflowAnomaly[]>(
      apiUrl(apiBase, "/signals/orderflow?limit=8"),
      "orderflow API",
    ),
  ]);

  return { dashboard, leaderboard, orderflow };
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "0.0%";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "unknown";
  }
  return dateFormatter.format(date);
}

function signalTypeLabel(type: OpportunitySignal["signal_type"]): string {
  if (type === "skilled_account") {
    return "Skilled account";
  }
  return "Order-flow dislocation";
}

function anomalyLabel(type: string): string {
  return type.replaceAll("_", " ");
}

function toneClasses(tone: OpportunityDriver["tone"]): string {
  if (tone === "positive") {
    return "border-emerald-200 bg-emerald-50 text-emerald-900";
  }
  if (tone === "risk") {
    return "border-rose-200 bg-rose-50 text-rose-900";
  }
  return "border-stone-200 bg-stone-50 text-stone-700";
}

function meterColor(value: number): string {
  if (value >= 0.85) {
    return "bg-emerald-600";
  }
  if (value >= 0.65) {
    return "bg-sky-600";
  }
  return "bg-amber-500";
}

function MetricCard({
  label,
  value,
  detail,
  accent,
}: {
  label: string;
  value: string;
  detail: string;
  accent: string;
}) {
  return (
    <div className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm">
      <div className={`mb-3 h-1.5 w-12 rounded ${accent}`} />
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-stone-950">{value}</p>
      <p className="mt-1 text-sm leading-5 text-stone-600">{detail}</p>
    </div>
  );
}

function Meter({ label, value }: { label: string; value: number }) {
  const width = `${Math.max(4, Math.min(100, value * 100))}%`;

  return (
    <div>
      <div className="flex items-center justify-between gap-3 text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
        <span>{label}</span>
        <span className="text-stone-800">{formatPercent(value)}</span>
      </div>
      <div
        aria-label={label}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.round(value * 100)}
        className="mt-2 h-2 rounded bg-stone-200"
        role="meter"
      >
        <div className={`h-2 rounded ${meterColor(value)}`} style={{ width }} />
      </div>
    </div>
  );
}

function OpportunityCard({ signal, rank }: { signal: OpportunitySignal; rank: number }) {
  return (
    <article className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
            <span>#{rank}</span>
            <span>{signalTypeLabel(signal.signal_type)}</span>
            {signal.market_ticker ? <span>{signal.market_ticker}</span> : null}
            {signal.direction ? <span>{signal.direction}</span> : null}
          </div>
          <h2 className="mt-2 text-xl font-semibold tracking-normal text-stone-950">{signal.title}</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-600">{signal.thesis}</p>
        </div>
        <div className="w-32 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-right">
          <p className="text-2xl font-semibold text-emerald-900">
            {formatPercent(signal.opportunity_score)}
          </p>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-emerald-700">
            EV score
          </p>
        </div>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Meter label="statistical probability" value={signal.probability} />
        <Meter label="model confidence" value={signal.confidence} />
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {signal.drivers.map((driver) => (
          <span
            className={`rounded-md border px-2.5 py-1 text-xs font-medium ${toneClasses(driver.tone)}`}
            key={`${signal.signal_id}-${driver.label}`}
          >
            {driver.label}: {driver.value}
          </span>
        ))}
      </div>

      <div className="mt-4 grid gap-3 border-t border-stone-200 pt-4 text-sm leading-6 text-stone-700 md:grid-cols-2">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
            Expected value basis
          </p>
          <p className="mt-1">{signal.expected_value_basis}</p>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
            Next check
          </p>
          <p className="mt-1">{signal.next_step}</p>
        </div>
      </div>

      <p className="mt-3 text-xs text-stone-500">Observed {formatDate(signal.observed_at)}</p>
    </article>
  );
}

function LeaderboardPanel({ rows }: { rows: LeaderboardRow[] }) {
  return (
    <section className="rounded-lg border border-stone-200 bg-white shadow-sm">
      <div className="border-b border-stone-200 p-4">
        <h2 className="text-base font-semibold text-stone-950">Skilled Accounts</h2>
        <p className="mt-1 text-sm text-stone-600">Accounts above expected resolved outcomes.</p>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left text-sm">
          <thead className="bg-stone-100 text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
            <tr>
              <th className="px-4 py-3">Account</th>
              <th className="px-4 py-3">Skill</th>
              <th className="px-4 py-3">Z</th>
              <th className="px-4 py-3">Calls</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-100">
            {rows.length === 0 ? (
              <tr>
                <td className="px-4 py-4 text-stone-500" colSpan={4}>
                  No qualifying accounts yet.
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={row.account_id}>
                  <td className="max-w-36 truncate px-4 py-3 font-medium text-stone-900">
                    {row.account_id}
                  </td>
                  <td className="px-4 py-3 text-stone-700">
                    {formatPercent(row.skill_likelihood)}
                  </td>
                  <td className="px-4 py-3 text-stone-700">
                    {row.stddevs_above_expected.toFixed(2)}
                  </td>
                  <td className="px-4 py-3 text-stone-700">
                    {numberFormatter.format(row.resolved_calls)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OrderflowPanel({ rows }: { rows: OrderflowAnomaly[] }) {
  return (
    <section className="rounded-lg border border-stone-200 bg-white shadow-sm">
      <div className="border-b border-stone-200 p-4">
        <h2 className="text-base font-semibold text-stone-950">Irregular Flow</h2>
        <p className="mt-1 text-sm text-stone-600">Recent volume and price anomalies.</p>
      </div>
      <div className="divide-y divide-stone-100">
        {rows.length === 0 ? (
          <p className="p-4 text-sm text-stone-500">No order-flow anomalies yet.</p>
        ) : (
          rows.map((row) => (
            <div className="p-4" key={`${row.market_ticker}-${row.trade_id}-${row.anomaly_type}`}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-medium text-stone-950">{row.market_ticker}</p>
                  <p className="mt-1 text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
                    {anomalyLabel(row.anomaly_type)} / {row.side}
                  </p>
                </div>
                <div className="text-right text-sm text-stone-700">
                  <p>{compactFormatter.format(row.quantity)} qty</p>
                  <p>{row.price.toFixed(2)}</p>
                </div>
              </div>
              <p className="mt-2 text-sm leading-5 text-stone-600">{row.details}</p>
              <p className="mt-2 text-xs text-stone-500">{formatDate(row.traded_at)}</p>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function ErrorBanner({ errors }: { errors: string[] }) {
  if (errors.length === 0) {
    return null;
  }

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
      <p className="font-semibold">Some signal feeds are unavailable.</p>
      <p className="mt-1">{errors.join(" | ")}</p>
    </div>
  );
}

export default async function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  const { dashboard, leaderboard, orderflow } = await getDashboardData(apiBase);
  const summary = dashboard.data?.summary ?? emptySummary;
  const opportunities = dashboard.data?.opportunities ?? [];
  const leaderboardRows = leaderboard.data ?? [];
  const orderflowRows = orderflow.data ?? [];
  const errors = [dashboard.error, leaderboard.error, orderflow.error].filter(
    (error): error is string => Boolean(error),
  );

  return (
    <div className="min-h-screen bg-[#f5f6f1] text-stone-950">
      <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <header className="grid gap-6 border-b border-stone-200 pb-6 lg:grid-cols-[1fr_360px]">
          <div>
            <div className="flex flex-wrap items-center gap-3 text-sm font-medium text-stone-600">
              <span>MarketSignalOS</span>
              <span className="h-1.5 w-1.5 rounded bg-emerald-600" />
              <span>{errors.length === 0 ? "API connected" : "API degraded"}</span>
            </div>
            <h1 className="mt-4 max-w-4xl text-4xl font-semibold tracking-normal text-stone-950 sm:text-5xl">
              Probability-ranked market inefficiency research.
            </h1>
            <p className="mt-4 max-w-3xl text-base leading-7 text-stone-600">
              The dashboard promotes candidates when abnormal account performance or trade-flow
              dislocation clears statistical thresholds, then keeps the evidence visible beside
              the score.
            </p>
          </div>
          <div className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-stone-500">
              Current model stance
            </p>
            <p className="mt-3 text-3xl font-semibold text-stone-950">
              {summary.candidate_count === 0 ? "No edge yet" : `${summary.candidate_count} signals`}
            </p>
            <p className="mt-2 text-sm leading-6 text-stone-600">
              Freshness window: {summary.freshness_window_days} days. Generated{" "}
              {dashboard.data?.generated_at ? formatDate(dashboard.data.generated_at) : "after API data arrives"}.
            </p>
          </div>
        </header>

        <div className="mt-6">
          <ErrorBanner errors={errors} />
        </div>

        <section className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard
            accent="bg-emerald-600"
            detail="Highest statistical edge probability in the active queue."
            label="Top probability"
            value={formatPercent(summary.top_probability)}
          />
          <MetricCard
            accent="bg-sky-600"
            detail="Composite rank from probability, confluence, and confidence."
            label="Top EV score"
            value={formatPercent(summary.top_opportunity_score)}
          />
          <MetricCard
            accent="bg-amber-500"
            detail={`${summary.skilled_account_count} account signals and ${summary.orderflow_event_count} flow events.`}
            label="Candidate count"
            value={numberFormatter.format(summary.candidate_count)}
          />
          <MetricCard
            accent="bg-rose-500"
            detail="Scores are research triage, not automated trade instructions."
            label="Risk posture"
            value="Manual review"
          />
        </section>

        <section className="mt-6 grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.75fr)]">
          <div className="space-y-4">
            <div>
              <h2 className="text-xl font-semibold text-stone-950">Opportunity Queue</h2>
              <p className="mt-1 text-sm text-stone-600">
                Ranked candidates with probability, confidence, and evidence drivers.
              </p>
            </div>

            {opportunities.length === 0 ? (
              <div className="rounded-lg border border-dashed border-stone-300 bg-white p-8 text-center">
                <p className="text-lg font-semibold text-stone-900">No qualifying signals yet.</p>
                <p className="mt-2 text-sm text-stone-600">
                  Add fills, resolutions, enrichment, or trade JSONL data to activate the queue.
                </p>
              </div>
            ) : (
              opportunities.map((signal, index) => (
                <OpportunityCard key={signal.signal_id} rank={index + 1} signal={signal} />
              ))
            )}
          </div>

          <aside className="space-y-5">
            <LeaderboardPanel rows={leaderboardRows} />
            <OrderflowPanel rows={orderflowRows} />
          </aside>
        </section>
      </main>
    </div>
  );
}
