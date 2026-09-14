export const dynamic = "force-dynamic";

import Link from "next/link";
import type { ReactElement } from "react";
import type { SkilledBet } from "../../components/SkilledBetsPanel";

type WalletBet = {
  proxy_wallet: string;
  condition_id: string;
  outcome_index: number;
  outcome: string;
  event_slug: string;
  category: string;
  title: string;
  status: string; // won | lost | exited | open
  entry_price: number;
  cost_usdc: number;
  net_size: number;
  realized_pnl_usdc: number;
  total_pnl_usdc: number;
  clv: number | null;
  last_trade_ts: number;
};

type EquityPoint = { ts: number; cumulative_pnl_usdc: number };

type CategoryAgg = {
  category: string;
  bets: number;
  wins: number;
  losses: number;
  open: number;
  pnl_usdc: number;
  win_rate: number;
};

type Enrichment = {
  proxy_wallet: string;
  name: string;
  skill_likelihood: number;
  edge_mean: number;
  edge_lower_bound: number;
  independent_settled_events: number;
  recent_skill_likelihood: number;
  recent_edge_mean: number;
  recent_independent_events: number;
  clv_mean: number;
  clv_lower_bound: number;
  clv_sample_size: number;
  wins: number;
  losses: number;
  win_rate: number;
  all_time_pnl_usdc: number;
  all_time_roi: number;
  pnl_30d_usdc: number;
  tailability_status: string;
  tailability_reasons: string[];
  computed_at: string;
  // Trading-style fields are absent on enrichment rows written before they
  // existed, hence optional.
  style_archetype?: string;
  automation_score?: number;
  style_drivers?: string[];
  trades_per_active_day?: number;
  median_intertrade_gap_seconds?: number;
  active_utc_hours?: number;
  buy_size_uniformity?: number;
  markets_per_active_day?: number;
  top_category?: string;
  top_category_share?: number;
  exited_position_share?: number;
  median_exit_hold_hours?: number;
};

type ExitRow = {
  condition_id: string;
  title: string;
  exit_type: string;
  reduction_pct: number;
  previous_value_usdc: number;
  detected_at: string;
};

type WalletDetail = {
  proxy_wallet: string;
  polymarket_profile_url: string;
  wallet_value_usdc: number;
  enrichment: Enrichment;
  open_positions: SkilledBet[];
  bet_history: WalletBet[];
  bet_history_total: number;
  equity_curve: EquityPoint[];
  category_breakdown: CategoryAgg[];
  exits: ExitRow[];
};

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});
const compactUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

function shortWallet(addr: string): string {
  if (addr.length < 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function pnlClass(value: number): string {
  if (value > 0) return "text-emerald-700";
  if (value < 0) return "text-red-700";
  return "text-zinc-500";
}

function formatDate(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10);
}

function EquityCurve({ points }: { points: EquityPoint[] }): ReactElement | null {
  if (points.length < 2) return null;
  const width = 720;
  const height = 160;
  const pad = 8;
  const values = points.map((p) => p.cumulative_pnl_usdc);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (points.length - 1)) * (width - 2 * pad);
  const y = (v: number) => height - pad - ((v - min) / span) * (height - 2 * pad);
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.cumulative_pnl_usdc).toFixed(1)}`)
    .join(" ");
  const zeroY = y(0);
  const last = values[values.length - 1];

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-zinc-900">Equity curve</h2>
        <span className={`font-mono text-sm font-semibold ${pnlClass(last)}`}>
          {last >= 0 ? "+" : ""}
          {usd.format(last)}
        </span>
      </div>
      <p className="mt-0.5 text-[11px] text-zinc-500">
        Cumulative PnL across {points.length} settled bets (resolution + realized exits),
        ordered by last fill
      </p>
      <svg
        className="mt-3 w-full"
        preserveAspectRatio="none"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        <line stroke="#e4e4e7" strokeDasharray="4 4" x1={pad} x2={width - pad} y1={zeroY} y2={zeroY} />
        <path d={path} fill="none" stroke={last >= 0 ? "#047857" : "#b91c1c"} strokeWidth="2" />
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[10px] text-zinc-400">
        <span>{formatDate(points[0].ts)}</span>
        <span>{formatDate(points[points.length - 1].ts)}</span>
      </div>
    </div>
  );
}

function statusBadge(status: string): string {
  switch (status) {
    case "won":
      return "bg-emerald-50 text-emerald-700";
    case "lost":
      return "bg-red-50 text-red-700";
    case "exited":
      return "bg-zinc-100 text-zinc-600";
    default:
      return "bg-blue-50 text-blue-700";
  }
}

function archetypeBadge(archetype: string): string {
  switch (archetype) {
    case "systematic":
      return "bg-cyan-50 text-cyan-700";
    case "mixed":
      return "bg-sky-50 text-sky-700";
    case "discretionary":
      return "bg-violet-50 text-violet-700";
    default:
      return "bg-zinc-100 text-zinc-500";
  }
}

function formatGapSeconds(seconds: number): string {
  if (seconds <= 0) return "—";
  if (seconds < 120) return `${seconds.toFixed(0)}s`;
  if (seconds < 7200) return `${(seconds / 60).toFixed(0)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function TradingStyleCard({ e }: { e: Enrichment }): ReactElement | null {
  const archetype = e.style_archetype ?? "unclassified";
  const drivers = e.style_drivers ?? [];
  if (archetype === "unclassified" && drivers.length === 0) return null;

  const metrics: Array<{ label: string; value: string }> = [
    {
      label: "Trades / active day",
      value: (e.trades_per_active_day ?? 0).toFixed(1),
    },
    {
      label: "Median trade gap",
      value: formatGapSeconds(e.median_intertrade_gap_seconds ?? 0),
    },
    { label: "UTC hours active", value: `${e.active_utc_hours ?? 0}/24` },
    {
      label: "Repeated-size buys",
      value: `${((e.buy_size_uniformity ?? 0) * 100).toFixed(0)}%`,
    },
    {
      label: "Markets / active day",
      value: (e.markets_per_active_day ?? 0).toFixed(1),
    },
    {
      label: "Top category",
      value: e.top_category
        ? `${e.top_category} (${((e.top_category_share ?? 0) * 100).toFixed(0)}%)`
        : "—",
    },
    {
      label: "Positions exited early",
      value: `${((e.exited_position_share ?? 0) * 100).toFixed(0)}%`,
    },
    {
      label: "Median exit hold",
      value:
        (e.median_exit_hold_hours ?? 0) > 0
          ? `${(e.median_exit_hold_hours ?? 0).toFixed(1)}h`
          : "—",
    },
  ];

  return (
    <div className="rounded-lg border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-zinc-900">Trading style</h2>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${archetypeBadge(archetype)}`}
          title="Descriptive only — labels the wallet's operational footprint, never gates tailability"
        >
          {archetype}
          {archetype !== "unclassified"
            ? ` · ${((e.automation_score ?? 0) * 100).toFixed(0)}%`
            : ""}
        </span>
      </div>
      {drivers.length > 0 && (
        <ul className="space-y-1 border-b border-zinc-100 px-4 py-3">
          {drivers.map((driver) => (
            <li className="text-[11px] text-zinc-600" key={driver}>
              · {driver}
            </li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-4 py-3">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dt className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
              {metric.label}
            </dt>
            <dd className="mt-0.5 font-mono text-xs font-semibold text-zinc-900">
              {metric.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

async function getWalletDetail(address: string): Promise<WalletDetail | null> {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
  try {
    const response = await fetch(
      `${apiBase.replace(/\/$/, "")}/signals/wallets/${encodeURIComponent(address)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return null;
    return (await response.json()) as WalletDetail;
  } catch {
    return null;
  }
}

export default async function WalletPage({
  params,
}: {
  params: Promise<{ address: string }>;
}) {
  const { address } = await params;
  const detail = await getWalletDetail(address);

  if (!detail) {
    return (
      <div className="min-h-screen bg-[#fafafa]">
        <main className="mx-auto max-w-4xl px-6 py-16">
          <p className="text-sm font-semibold text-zinc-900">Wallet not found</p>
          <p className="mt-1.5 text-xs text-zinc-500">
            No enrichment data for <span className="font-mono">{address}</span>. Run an
            ingest, or check the address.
          </p>
          <Link className="mt-4 inline-block text-sm font-semibold text-emerald-700 hover:underline" href="/">
            ← Back to dashboard
          </Link>
        </main>
      </div>
    );
  }

  const e = detail.enrichment;
  const stats: Array<{ label: string; value: string; cls?: string; title?: string }> = [
    { label: "Forecast confidence", value: pct(e.skill_likelihood), title: "P(edge > 0 | data)" },
    {
      label: "Edge (log-odds)",
      value: `${e.edge_mean >= 0 ? "+" : ""}${e.edge_mean.toFixed(2)} [≥${e.edge_lower_bound.toFixed(2)}]`,
      cls: e.edge_lower_bound > 0 ? "text-emerald-700" : "text-zinc-900",
    },
    {
      label: "Recent edge (180d half-life)",
      value: `${e.recent_edge_mean >= 0 ? "+" : ""}${e.recent_edge_mean.toFixed(2)} · ${e.recent_independent_events.toFixed(1)} ev`,
    },
    {
      label: "CLV",
      value:
        e.clv_sample_size > 0
          ? `${e.clv_mean >= 0 ? "+" : ""}${(e.clv_mean * 100).toFixed(1)}¢ · ${e.clv_sample_size.toFixed(1)} ev`
          : "no price history yet",
      cls: e.clv_sample_size > 0 && e.clv_lower_bound > 0 ? "text-emerald-700" : undefined,
      title: "Avg (closing/current line − entry) of the picked outcome, event-capped",
    },
    { label: "Record", value: `${e.wins}W / ${e.losses}L (${pct(e.win_rate)})` },
    {
      label: "All-time PnL",
      value: `${e.all_time_pnl_usdc >= 0 ? "+" : ""}${compactUsd.format(e.all_time_pnl_usdc)} (${pct(e.all_time_roi)} ROI)`,
      cls: pnlClass(e.all_time_pnl_usdc),
    },
    {
      label: "30d PnL",
      value: `${e.pnl_30d_usdc >= 0 ? "+" : ""}${compactUsd.format(e.pnl_30d_usdc)}`,
      cls: pnlClass(e.pnl_30d_usdc),
    },
    {
      label: "Bankroll",
      value: detail.wallet_value_usdc > 0 ? compactUsd.format(detail.wallet_value_usdc) : "—",
    },
  ];

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <nav className="sticky top-0 z-10 border-b border-zinc-200 bg-white/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <Link className="text-sm font-semibold tracking-tight text-zinc-900 hover:underline" href="/">
            ← MarketSignalOS
          </Link>
          <a
            className="text-xs font-semibold text-zinc-500 hover:text-zinc-900 hover:underline"
            href={detail.polymarket_profile_url}
            rel="noopener noreferrer"
            target="_blank"
          >
            View on Polymarket ↗
          </a>
        </div>
      </nav>

      <main className="mx-auto max-w-7xl space-y-6 px-6 pb-16 pt-8">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
              {e.name || shortWallet(detail.proxy_wallet)}
            </h1>
            <span
              className={`rounded px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${
                e.tailability_status === "tailable"
                  ? "bg-emerald-50 text-emerald-700"
                  : "bg-amber-50 text-amber-800"
              }`}
              title={e.tailability_reasons.join(", ")}
            >
              {e.tailability_status}
            </span>
            {(e.style_archetype ?? "unclassified") !== "unclassified" && (
              <span
                className={`rounded px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${archetypeBadge(e.style_archetype ?? "")}`}
                title={(e.style_drivers ?? []).join(", ")}
              >
                {e.style_archetype}
              </span>
            )}
          </div>
          <p className="mt-1 font-mono text-xs text-zinc-400">{detail.proxy_wallet}</p>
          {e.tailability_reasons.length > 0 && (
            <p className="mt-2 max-w-3xl text-xs text-amber-800">
              {e.tailability_reasons.join(" · ")}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.map((stat) => (
            <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3" key={stat.label} title={stat.title}>
              <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                {stat.label}
              </p>
              <p className={`mt-1 font-mono text-sm font-semibold ${stat.cls ?? "text-zinc-900"}`}>
                {stat.value}
              </p>
            </div>
          ))}
        </div>

        <EquityCurve points={detail.equity_curve} />

        <div className="grid gap-6 xl:grid-cols-[1fr_340px]">
          <section className="space-y-6">
            <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
              <div className="border-b border-zinc-100 px-4 py-3">
                <h2 className="text-sm font-semibold text-zinc-900">
                  Bet history
                  <span className="ml-2 font-mono text-[11px] font-normal text-zinc-400">
                    {detail.bet_history.length} of {detail.bet_history_total}
                  </span>
                </h2>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full text-left">
                  <thead>
                    <tr className="border-b border-zinc-100">
                      <th className="px-4 py-2 text-xs font-medium text-zinc-400">Market</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Side</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Entry</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Cost</th>
                      <th
                        className="px-4 py-2 text-right text-xs font-medium text-zinc-400"
                        title="Closing (or current) line minus entry, in cents"
                      >
                        CLV
                      </th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">PnL</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Status</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Last fill</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100">
                    {detail.bet_history.length === 0 ? (
                      <tr>
                        <td className="px-4 py-4 text-xs text-zinc-400" colSpan={8}>
                          No bet records yet — run an ingest to populate.
                        </td>
                      </tr>
                    ) : (
                      detail.bet_history.map((bet) => (
                        <tr
                          className="hover:bg-zinc-50"
                          key={`${bet.condition_id}-${bet.outcome_index}`}
                        >
                          <td className="max-w-[260px] truncate px-4 py-2 text-xs font-medium text-zinc-900" title={bet.title}>
                            {bet.title || bet.event_slug || bet.condition_id}
                            {bet.category && (
                              <span className="ml-1.5 text-[10px] uppercase text-zinc-400">
                                {bet.category}
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs text-zinc-700">
                            {bet.outcome || (bet.outcome_index === 0 ? "Yes" : "No")}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs text-zinc-700">
                            ${bet.entry_price.toFixed(2)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs text-zinc-700">
                            {usd.format(bet.cost_usdc)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs">
                            {bet.clv === null ? (
                              <span className="text-zinc-300">—</span>
                            ) : (
                              <span className={bet.clv >= 0 ? "text-emerald-700" : "text-red-700"}>
                                {bet.clv >= 0 ? "+" : ""}
                                {(bet.clv * 100).toFixed(1)}¢
                              </span>
                            )}
                          </td>
                          <td className={`px-4 py-2 text-right font-mono text-xs font-semibold ${pnlClass(bet.total_pnl_usdc)}`}>
                            {bet.status === "open"
                              ? "—"
                              : `${bet.total_pnl_usdc >= 0 ? "+" : ""}${usd.format(bet.total_pnl_usdc)}`}
                          </td>
                          <td className="px-4 py-2 text-right">
                            <span
                              className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${statusBadge(bet.status)}`}
                            >
                              {bet.status}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-[11px] text-zinc-400">
                            {formatDate(bet.last_trade_ts)}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </section>

          <aside className="space-y-4">
            <TradingStyleCard e={e} />

            {detail.open_positions.length > 0 && (
              <div className="rounded-lg border border-zinc-200 bg-white">
                <div className="border-b border-zinc-100 px-4 py-3">
                  <h2 className="text-sm font-semibold text-zinc-900">
                    Open positions ({detail.open_positions.length})
                  </h2>
                </div>
                <ul className="divide-y divide-zinc-100">
                  {detail.open_positions.map((pos) => (
                    <li className="px-4 py-2.5" key={`${pos.condition_id}-${pos.outcome_index}`}>
                      <p className="truncate text-xs font-medium text-zinc-900" title={pos.title}>
                        {pos.title}
                      </p>
                      <p className="mt-0.5 font-mono text-[11px] text-zinc-500">
                        {pos.outcome || (pos.outcome_index === 0 ? "Yes" : "No")} · entry $
                        {pos.entry_price.toFixed(2)} · {usd.format(pos.current_position_value_usdc)}
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="rounded-lg border border-zinc-200 bg-white">
              <div className="border-b border-zinc-100 px-4 py-3">
                <h2 className="text-sm font-semibold text-zinc-900">By category</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full text-left">
                  <thead>
                    <tr className="border-b border-zinc-100">
                      <th className="px-4 py-2 text-xs font-medium text-zinc-400">Category</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Bets</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">W/L</th>
                      <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">PnL</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100">
                    {detail.category_breakdown.map((cat) => (
                      <tr key={cat.category}>
                        <td className="px-4 py-2 text-xs font-medium text-zinc-900">{cat.category}</td>
                        <td className="px-4 py-2 text-right font-mono text-xs text-zinc-700">{cat.bets}</td>
                        <td className="px-4 py-2 text-right font-mono text-xs text-zinc-700">
                          {cat.wins}/{cat.losses}
                        </td>
                        <td className={`px-4 py-2 text-right font-mono text-xs font-semibold ${pnlClass(cat.pnl_usdc)}`}>
                          {cat.pnl_usdc >= 0 ? "+" : ""}
                          {compactUsd.format(cat.pnl_usdc)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {detail.exits.length > 0 && (
              <div className="rounded-lg border border-zinc-200 bg-white">
                <div className="border-b border-zinc-100 px-4 py-3">
                  <h2 className="text-sm font-semibold text-zinc-900">Recent exits</h2>
                </div>
                <ul className="divide-y divide-zinc-100">
                  {detail.exits.slice(0, 8).map((exit) => (
                    <li className="px-4 py-2.5" key={`${exit.condition_id}-${exit.detected_at}`}>
                      <p className="truncate text-xs font-medium text-zinc-900" title={exit.title}>
                        {exit.title}
                      </p>
                      <p className="mt-0.5 font-mono text-[11px] text-zinc-500">
                        {exit.exit_type === "closed"
                          ? "closed"
                          : `trimmed ${(exit.reduction_pct * 100).toFixed(0)}%`}{" "}
                        · was {usd.format(exit.previous_value_usdc)}
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </aside>
        </div>
      </main>
    </div>
  );
}
