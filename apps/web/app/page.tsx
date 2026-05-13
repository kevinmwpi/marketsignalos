export const dynamic = "force-dynamic";

import CrossExchangePanel, { type CrossExchangeSignal } from "./components/CrossExchangePanel";
import IngestButton from "./components/IngestButton";
import PolymarketLeaderboardPanel, { type PolymarketWalletSkill } from "./components/PolymarketLeaderboardPanel";

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

type ProfileSummary = {
  username: string;
  snapshot_count: number;
  first_seen_at: string;
  last_seen_at: string;
  latest_rank: number | null;
  latest_win_rate: number | null;
  latest_profit_dollars: number | null;
  latest_resolved_trades: number | null;
  skill_likelihood: number | null;
  categories: string[];
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
  profiles: ApiResult<ProfileSummary[]>;
  polymarketLeaderboard: ApiResult<PolymarketWalletSkill[]>;
  crossExchange: ApiResult<CrossExchangeSignal[]>;
}> {
  const [
    dashboard,
    leaderboard,
    orderflow,
    profiles,
    polymarketLeaderboard,
    crossExchange,
  ] = await Promise.all([
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
    fetchJson<ProfileSummary[]>(
      apiUrl(apiBase, "/signals/profiles?limit=50"),
      "profiles API",
    ),
    fetchJson<PolymarketWalletSkill[]>(
      apiUrl(apiBase, "/signals/polymarket-leaderboard?min_resolved=5&limit=10"),
      "polymarket leaderboard API",
    ),
    fetchJson<CrossExchangeSignal[]>(
      apiUrl(apiBase, "/signals/cross-exchange?min_skill=0.6&min_resolved=5&min_dislocation_pct=2&limit=15"),
      "cross-exchange API",
    ),
  ]);

  return { dashboard, leaderboard, orderflow, profiles, polymarketLeaderboard, crossExchange };
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0.0%";
  return `${(value * 100).toFixed(1)}%`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return dateFormatter.format(date);
}

function signalTypeLabel(type: OpportunitySignal["signal_type"]): string {
  return type === "skilled_account" ? "Skilled account" : "Order-flow";
}

function anomalyLabel(type: string): string {
  return type.replaceAll("_", " ");
}

function toneClasses(tone: OpportunityDriver["tone"]): string {
  if (tone === "positive") return "bg-emerald-50 text-emerald-700";
  if (tone === "risk") return "bg-red-50 text-red-700";
  return "bg-zinc-100 text-zinc-600";
}

function meterColor(value: number): string {
  if (value >= 0.85) return "bg-emerald-500";
  if (value >= 0.65) return "bg-blue-500";
  return "bg-amber-400";
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="bg-white p-5">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">{label}</p>
      <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-zinc-900">{value}</p>
      <p className="mt-1 text-xs leading-relaxed text-zinc-500">{detail}</p>
    </div>
  );
}

function Meter({ label, value }: { label: string; value: number }) {
  const width = `${Math.max(4, Math.min(100, value * 100))}%`;

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium text-zinc-500">{label}</span>
        <span className="font-mono text-xs font-semibold text-zinc-700">{formatPercent(value)}</span>
      </div>
      <div
        aria-label={label}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.round(value * 100)}
        className="h-1 rounded-full bg-zinc-100"
        role="meter"
      >
        <div className={`h-1 rounded-full ${meterColor(value)}`} style={{ width }} />
      </div>
    </div>
  );
}

function OpportunityCard({ signal, rank }: { signal: OpportunitySignal; rank: number }) {
  return (
    <article className="rounded-lg border border-zinc-200 bg-white p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-xs text-zinc-400">#{rank}</span>
            <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-600">
              {signalTypeLabel(signal.signal_type)}
            </span>
            {signal.market_ticker ? (
              <span className="font-mono text-xs font-semibold text-zinc-700">
                {signal.market_ticker}
              </span>
            ) : null}
            {signal.direction ? (
              <span
                className={`rounded px-1.5 py-0.5 text-xs font-semibold ${
                  signal.direction === "yes"
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-red-50 text-red-700"
                }`}
              >
                {signal.direction.toUpperCase()}
              </span>
            ) : null}
          </div>
          <h2 className="mt-2.5 text-base font-semibold leading-snug text-zinc-900">
            {signal.title}
          </h2>
          <p className="mt-1.5 text-sm leading-relaxed text-zinc-500">{signal.thesis}</p>
        </div>
        <div className="shrink-0 text-right">
          <p className="font-mono text-2xl font-semibold tabular-nums text-zinc-900">
            {formatPercent(signal.opportunity_score)}
          </p>
          <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
            EV score
          </p>
        </div>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <Meter label="Statistical probability" value={signal.probability} />
        <Meter label="Model confidence" value={signal.confidence} />
      </div>

      <div className="mt-4 flex flex-wrap gap-1.5">
        {signal.drivers.map((driver) => (
          <span
            className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium ${toneClasses(driver.tone)}`}
            key={`${signal.signal_id}-${driver.label}`}
          >
            {driver.label}
            <span className="opacity-50">·</span>
            {driver.value}
          </span>
        ))}
      </div>

      <div className="mt-4 grid gap-4 border-t border-zinc-100 pt-4 sm:grid-cols-2">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
            EV basis
          </p>
          <p className="mt-1 text-sm leading-relaxed text-zinc-600">{signal.expected_value_basis}</p>
        </div>
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
            Next check
          </p>
          <p className="mt-1 text-sm leading-relaxed text-zinc-600">{signal.next_step}</p>
        </div>
      </div>

      <p className="mt-3 font-mono text-[11px] text-zinc-400">
        Observed {formatDate(signal.observed_at)}
      </p>
    </article>
  );
}

function LeaderboardPanel({ rows }: { rows: LeaderboardRow[] }) {
  return (
    <section className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-200 px-4 py-3">
        <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
          Skilled Accounts
        </h2>
        <p className="mt-0.5 text-xs text-zinc-500">Above-expected resolved outcomes</p>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left">
          <thead>
            <tr className="border-b border-zinc-100">
              <th className="px-4 py-2 text-left text-xs font-medium text-zinc-400">Account</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Skill</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Z</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Calls</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.length === 0 ? (
              <tr>
                <td className="px-4 py-4 text-xs text-zinc-400" colSpan={4}>
                  No qualifying accounts yet.
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr className="hover:bg-zinc-50" key={row.account_id}>
                  <td className="px-4 py-2.5">
                    <span className="block max-w-28 truncate font-mono text-xs font-medium text-zinc-900">
                      {row.account_id}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {formatPercent(row.skill_likelihood)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {row.stddevs_above_expected.toFixed(2)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
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
    <section className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-200 px-4 py-3">
        <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
          Irregular Flow
        </h2>
        <p className="mt-0.5 text-xs text-zinc-500">Volume and price anomalies</p>
      </div>
      <div className="divide-y divide-zinc-100">
        {rows.length === 0 ? (
          <p className="px-4 py-4 text-xs text-zinc-400">No anomalies detected.</p>
        ) : (
          rows.map((row) => (
            <div
              className="px-4 py-3 hover:bg-zinc-50"
              key={`${row.market_ticker}-${row.trade_id}-${row.anomaly_type}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-semibold text-zinc-900">
                    {row.market_ticker}
                  </p>
                  <p className="mt-0.5 text-xs text-zinc-500">
                    {anomalyLabel(row.anomaly_type)} · {row.side}
                  </p>
                </div>
                <div className="shrink-0 text-right">
                  <p className="font-mono text-xs font-semibold text-zinc-700">
                    {compactFormatter.format(row.quantity)} qty
                  </p>
                  <p className="font-mono text-xs text-zinc-500">{row.price.toFixed(2)}</p>
                </div>
              </div>
              <p className="mt-1.5 text-xs leading-relaxed text-zinc-500">{row.details}</p>
              <p className="mt-1.5 font-mono text-[11px] text-zinc-400">
                {formatDate(row.traded_at)}
              </p>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function ProfilesSection({ profiles }: { profiles: ProfileSummary[] }) {
  return (
    <section className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-200 px-4 py-3">
        <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
          Tracked Profiles
        </h2>
        <p className="mt-0.5 text-xs text-zinc-500">
          Scraped via Playwright · Enable with{" "}
          <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-[10px]">
            INGEST_SCRAPE_LEADERBOARD=1
          </code>
        </p>
      </div>
      {profiles.length === 0 ? (
        <p className="px-4 py-6 text-xs text-zinc-400">
          No scraped profiles yet. Run the ingestor with{" "}
          <code className="font-mono">INGEST_SCRAPE_LEADERBOARD=1</code> to begin collecting
          data.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left">
            <thead>
              <tr className="border-b border-zinc-100">
                <th className="px-4 py-2 text-left text-xs font-medium text-zinc-400">Username</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Rank</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Skill</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Win rate</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Profit</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Resolved</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Snapshots</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Last seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {profiles.map((p) => (
                <tr className="hover:bg-zinc-50" key={p.username}>
                  <td className="px-4 py-2.5">
                    <span className="font-mono text-xs font-medium text-zinc-900">{p.username}</span>
                    {p.categories.length > 0 && (
                      <span className="ml-2 text-[10px] text-zinc-400">
                        {p.categories.slice(0, 2).join(", ")}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {p.latest_rank != null ? `#${p.latest_rank}` : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs">
                    {p.skill_likelihood != null ? (
                      <span className={p.skill_likelihood >= 0.95 ? "font-semibold text-emerald-700" : "text-zinc-700"}>
                        {formatPercent(p.skill_likelihood)}
                      </span>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {p.latest_win_rate != null ? formatPercent(p.latest_win_rate) : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {p.latest_profit_dollars != null
                      ? `$${numberFormatter.format(Math.round(p.latest_profit_dollars))}`
                      : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {p.latest_resolved_trades != null
                      ? numberFormatter.format(p.latest_resolved_trades)
                      : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-500">
                    {p.snapshot_count}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-400">
                    {p.last_seen_at ? formatDate(p.last_seen_at) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
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
  const { dashboard, leaderboard, orderflow, profiles, polymarketLeaderboard, crossExchange } =
    await getDashboardData(apiBase);
  const summary = dashboard.data?.summary ?? emptySummary;
  const opportunities = dashboard.data?.opportunities ?? [];
  const leaderboardRows = leaderboard.data ?? [];
  const orderflowRows = orderflow.data ?? [];
  const profileRows = profiles.data ?? [];
  const polymarketLeaderboardRows = polymarketLeaderboard.data ?? [];
  const crossExchangeSignals = crossExchange.data ?? [];
  const crossExchangeGeneratedAt =
    crossExchangeSignals.length > 0 ? crossExchangeSignals[0].observed_at : null;
  const errors = [
    dashboard.error,
    leaderboard.error,
    orderflow.error,
    polymarketLeaderboard.error,
    crossExchange.error,
  ].filter((error): error is string => Boolean(error));
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
              <a className="text-zinc-500 hover:text-zinc-900" href="/skilled-bets">
                Skilled bets
              </a>
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
        {/* Page header */}
        <div className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
            Signal Intelligence
          </h1>
          <p className="mt-1.5 text-sm text-zinc-500">
            {summary.candidate_count > 0
              ? `${summary.candidate_count} active signal${summary.candidate_count !== 1 ? "s" : ""}`
              : "No active signals"}
            {" · "}
            {summary.freshness_window_days}-day window
            {dashboard.data?.generated_at
              ? ` · Updated ${formatDate(dashboard.data.generated_at)}`
              : ""}
          </p>
        </div>

        <ErrorBanner errors={errors} />

        {/* Metrics strip */}
        <section
          aria-label="Summary metrics"
          className="mb-8 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-zinc-200 bg-zinc-200 xl:grid-cols-4"
        >
          <MetricCard
            detail="Highest edge probability in the active queue."
            label="Top probability"
            value={formatPercent(summary.top_probability)}
          />
          <MetricCard
            detail="Composite score: probability × confluence × confidence."
            label="Top EV score"
            value={formatPercent(summary.top_opportunity_score)}
          />
          <MetricCard
            detail={`${summary.skilled_account_count} account · ${summary.orderflow_event_count} flow`}
            label="Candidates"
            value={numberFormatter.format(summary.candidate_count)}
          />
          <MetricCard
            detail="Research triage only — not automated trade instructions."
            label="Risk posture"
            value="Manual review"
          />
        </section>

        {/* Cross-exchange dislocations — the marquee signal */}
        <div className="mb-8">
          <CrossExchangePanel
            generatedAt={crossExchangeGeneratedAt}
            signals={crossExchangeSignals}
          />
        </div>

        {/* Main two-column layout */}
        <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
          {/* Opportunity Queue */}
          <section>
            <div className="mb-4 flex items-baseline justify-between">
              <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                Opportunity Queue
              </h2>
              {opportunities.length > 0 && (
                <span className="font-mono text-xs text-zinc-400">{opportunities.length} signals</span>
              )}
            </div>

            {opportunities.length === 0 ? (
              <div className="rounded-lg border border-dashed border-zinc-300 bg-white p-10 text-center">
                <p className="text-sm font-semibold text-zinc-900">No qualifying signals</p>
                <p className="mt-1.5 text-xs leading-relaxed text-zinc-500">
                  Add fills, resolutions, and enrichment data to activate the queue.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {opportunities.map((signal, index) => (
                  <OpportunityCard key={signal.signal_id} rank={index + 1} signal={signal} />
                ))}
              </div>
            )}
          </section>

          {/* Sidebar */}
          <aside className="space-y-4">
            <PolymarketLeaderboardPanel rows={polymarketLeaderboardRows} />
            <LeaderboardPanel rows={leaderboardRows} />
            <OrderflowPanel rows={orderflowRows} />
          </aside>
        </div>

        {/* Scraped profiles section */}
        <div className="mt-6">
          <ProfilesSection profiles={profileRows} />
        </div>
      </main>
    </div>
  );
}
