import Link from "next/link";
import type { ReactElement } from "react";

export type PolymarketWalletSkill = {
  proxy_wallet: string;
  name: string;
  pseudonym: string;
  resolved_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  skill_likelihood: number;           // P(edge > 0 | data)
  stddevs_above_expected: number;     // edge_mean / sqrt(edge_var)
  edge_mean: number;                  // posterior edge in log-odds
  edge_lower_bound: number;           // 5th-percentile lower bound
  effective_sample_size: number;
  resolved_volume_usdc: number;
  rank_score: number;                 // canonical ranking metric
  forecast_skill_likelihood: number;
  forecast_edge_mean: number;
  forecast_edge_lower_bound: number;
  independent_settled_events: number;
  all_time_pnl_usdc: number;
  all_time_volume_usdc: number;
  all_time_roi: number;
  pnl_30d_usdc: number;
  active_pnl_usdc: number;
  max_drawdown_usdc: number;
  data_quality_status: string;
  data_quality_reasons: string[];
  economic_qualified: boolean;
  tailability_status: string;
  tailability_reasons: string[];
  score_version: string;
  style_archetype: string;
  automation_score: number;
  style_drivers: string[];
  total_volume_usdc: number;
  total_pnl_usdc: number;
  avg_position_size_usdc: number;
  trade_count: number;
  last_activity_at: number;
  computed_at: string;
  polymarket_profile_url: string;
};

const numberFormatter = new Intl.NumberFormat("en-US");
const compactUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

function formatPct(value: number): string {
  if (!Number.isFinite(value)) return "0.0%";
  return `${(value * 100).toFixed(1)}%`;
}

function formatRelativeSeconds(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  const diff = Math.round(Date.now() / 1000 - unixSeconds);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function formatRelativeIso(iso: string | null): string {
  if (!iso) return "";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "";
  const diff = Math.round((Date.now() - ts) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function shortWallet(addr: string): string {
  if (addr.length < 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function pnlClass(pnl: number): string {
  if (pnl > 0) return "text-emerald-700";
  if (pnl < 0) return "text-red-700";
  return "text-zinc-500";
}

export default function PolymarketLeaderboardPanel({
  rows,
}: {
  rows: PolymarketWalletSkill[];
}): ReactElement {
  const computedAt = rows.length > 0 ? rows[0].computed_at : null;

  return (
    <section className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-200 px-4 py-3">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
            Polymarket Wallet Research
          </h2>
          {computedAt && (
            <span className="font-mono text-[10px] text-zinc-400">
              {formatRelativeIso(computedAt)}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-zinc-500">Posterior edge vs. market-implied odds</p>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left">
          <thead>
            <tr className="border-b border-zinc-100">
              <th className="px-4 py-2 text-left text-xs font-medium text-zinc-400">Wallet</th>
              <th
                className="px-4 py-2 text-right text-xs font-medium text-zinc-400"
                title="P(edge > 0 | observed bets) — shrunk toward the population prior"
              >
                Forecast
              </th>
              <th
                className="px-4 py-2 text-right text-xs font-medium text-zinc-400"
                title="Posterior edge in log-odds vs. the market's own implied price (lower bound is 5th-percentile)"
              >
                Edge
              </th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">W/L</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Independent</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">PnL</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">ROI</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">30d PnL</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Active</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.length === 0 ? (
              <tr>
                <td className="px-4 py-4 text-xs text-zinc-400" colSpan={9}>
                  No qualifying wallets yet. Run the Polymarket ingestor to populate.
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr className="hover:bg-zinc-50" key={row.proxy_wallet}>
                  <td className="px-4 py-2.5">
                    <Link
                      className="group block focus:outline-none focus:ring-2 focus:ring-emerald-400"
                      href={`/wallet/${row.proxy_wallet}`}
                      title="Open wallet detail"
                    >
                      <span className="block font-mono text-xs font-medium text-zinc-900 group-hover:underline">
                        {row.name || shortWallet(row.proxy_wallet)}
                      </span>
                      {row.name && (
                        <span className="block font-mono text-[10px] text-zinc-400">
                          {shortWallet(row.proxy_wallet)}
                        </span>
                      )}
                      <span
                        className={`mt-1 inline-block rounded px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
                          row.tailability_status === "tailable"
                            ? "bg-emerald-50 text-emerald-700"
                            : "bg-amber-50 text-amber-700"
                        }`}
                        title={row.tailability_reasons.join(", ")}
                      >
                        {row.tailability_status}
                      </span>
                      {(row.style_archetype === "systematic" ||
                        row.style_archetype === "mixed") && (
                        <span
                          className={`ml-1 mt-1 inline-block rounded px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
                            row.style_archetype === "systematic"
                              ? "bg-cyan-50 text-cyan-700"
                              : "bg-sky-50 text-sky-700"
                          }`}
                          title={(row.style_drivers ?? []).join(", ")}
                        >
                          {row.style_archetype}
                        </span>
                      )}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {formatPct(row.forecast_skill_likelihood || row.skill_likelihood)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs">
                    <span
                      className={
                        row.edge_lower_bound > 0
                          ? "font-semibold text-emerald-700"
                          : "text-zinc-700"
                      }
                    >
                      {row.edge_mean >= 0 ? "+" : ""}
                      {row.edge_mean.toFixed(2)}
                    </span>
                    <span className="ml-1 text-[10px] text-zinc-400">
                      [≥{row.edge_lower_bound >= 0 ? "+" : ""}
                      {row.edge_lower_bound.toFixed(2)}]
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {numberFormatter.format(row.wins)}/{numberFormatter.format(row.losses)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {row.independent_settled_events.toFixed(1)}
                  </td>
                  <td
                    className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${pnlClass(row.total_pnl_usdc)}`}
                  >
                    {row.all_time_pnl_usdc >= 0 ? "+" : ""}
                    {compactUsd.format(row.all_time_pnl_usdc)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {formatPct(row.all_time_roi)}
                  </td>
                  <td
                    className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${pnlClass(row.pnl_30d_usdc)}`}
                  >
                    {row.pnl_30d_usdc >= 0 ? "+" : ""}
                    {compactUsd.format(row.pnl_30d_usdc)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-400">
                    {formatRelativeSeconds(row.last_activity_at)}
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
