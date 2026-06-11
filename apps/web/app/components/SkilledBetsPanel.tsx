import Link from "next/link";
import type { ReactElement } from "react";

export type SkilledBet = {
  proxy_wallet: string;
  wallet_name: string;
  skill_likelihood: number;
  resolved_trades: number;
  win_rate: number;
  edge_mean: number;
  edge_lower_bound: number;
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

  condition_id: string;
  slug: string;
  event_slug: string;
  title: string;
  category: string;

  outcome_index: number;
  outcome: string;
  entry_price: number;
  entry_size: number;
  entry_usdc_size: number;
  transaction_hash: string;
  bought_at: number;
  signal_age_days: number;

  current_position_size: number;
  current_position_value_usdc: number;
  current_market_yes_price: number;
  current_outcome_price: number;

  polymarket_profile_url: string;
  polymarket_market_url: string;

  kalshi_ticker: string;
  kalshi_event_ticker: string;
  kalshi_title: string;
  kalshi_market_url: string;
  kalshi_yes_price: number;
  kalshi_match_confidence: number;
  kalshi_match_status: string;
  kalshi_vs_entry_cents: number;

  tradability: string;
  tradability_reasons: string[];

  move_captured_pct: number;
  remaining_edge_status: string;

  consensus_wallets: number;
  consensus_contested: boolean;

  conviction: string;
  conviction_z: number;
  entry_pct_of_bankroll: number;

  clv_mean: number;
  clv_lower_bound: number;
  clv_sample_size: number;
};

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const shareFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

const TRADABILITY_LABELS: Record<string, string> = {
  poly_direct: "Tail on Polymarket",
  kalshi_mirror: "Tail on Kalshi",
  on_chain_only: "On-chain only",
  closed: "Closed / unavailable",
};

function shortWallet(addr: string): string {
  if (addr.length < 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function formatRelativeSeconds(unixSeconds: number): string {
  if (!unixSeconds) return "—";
  const diff = Math.round(Date.now() / 1000 - unixSeconds);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function driftPct(entry: number, current: number): number | null {
  if (!current || !entry) return null;
  return ((current - entry) / entry) * 100;
}

function driftColor(drift: number | null): string {
  if (drift === null) return "text-zinc-400";
  if (drift > 0) return "text-emerald-700";
  if (drift < 0) return "text-red-700";
  return "text-zinc-500";
}

const REMAINING_EDGE_LABELS: Record<string, string> = {
  discounted: "Below entry",
  fresh: "Fresh entry",
  partial: "Partially priced",
  late: "Late",
};

function remainingEdgeBadgeClass(status: string): string {
  switch (status) {
    case "discounted":
    case "fresh":
      return "bg-emerald-50 text-emerald-700";
    case "partial":
      return "bg-amber-50 text-amber-800";
    case "late":
      return "bg-red-50 text-red-700";
    default:
      return "bg-zinc-100 text-zinc-500";
  }
}

function tradabilityBadgeClass(status: string): string {
  switch (status) {
    case "poly_direct":
      return "bg-emerald-50 text-emerald-800";
    case "kalshi_mirror":
      return "bg-blue-50 text-blue-800";
    case "closed":
      return "bg-zinc-100 text-zinc-600";
    default:
      return "bg-amber-50 text-amber-800";
  }
}

export default function SkilledBetsPanel({
  bets,
}: {
  bets: SkilledBet[];
}): ReactElement {
  if (bets.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-white p-10 text-center">
        <p className="text-sm font-semibold text-zinc-900">No actionable skilled bets yet</p>
        <p className="mt-1.5 text-xs leading-relaxed text-zinc-500">
          No skilled wallets are holding positions you can tail on Polymarket or via an
          approved Kalshi mirror. Run ingest to refresh, or widen filters with{" "}
          <span className="font-mono">include_untradable=true</span> on the API.
        </p>
      </div>
    );
  }

  return (
    <ul className="space-y-3">
      {bets.map((bet, index) => {
        const drift = driftPct(bet.entry_price, bet.current_outcome_price);
        const outcomeLabel =
          bet.outcome || (bet.outcome_index === 0 ? "YES" : "NO");
        const showKalshiCta =
          bet.tradability === "kalshi_mirror" &&
          bet.kalshi_match_status === "approved" &&
          bet.kalshi_market_url;
        const showPolyCta =
          bet.tradability === "poly_direct" && bet.polymarket_market_url;

        return (
          <li
            className="overflow-hidden rounded-lg border border-zinc-200 bg-white"
            key={`${bet.proxy_wallet}-${bet.condition_id}-${bet.outcome_index}-${bet.bought_at}`}
          >
            <div className="px-5 py-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-zinc-400">#{index + 1}</span>
                    <span
                      className={[
                        "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                        bet.outcome_index === 0
                          ? "bg-emerald-50 text-emerald-700"
                          : "bg-red-50 text-red-700",
                      ].join(" ")}
                    >
                      {outcomeLabel}
                    </span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tradabilityBadgeClass(bet.tradability)}`}
                    >
                      {TRADABILITY_LABELS[bet.tradability] ?? bet.tradability}
                    </span>
                    {bet.remaining_edge_status !== "unknown" &&
                      REMAINING_EDGE_LABELS[bet.remaining_edge_status] && (
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${remainingEdgeBadgeClass(bet.remaining_edge_status)}`}
                          title="Fraction of the entry-to-$1 move the market has already made since the wallet bought"
                        >
                          {REMAINING_EDGE_LABELS[bet.remaining_edge_status]}
                          {bet.move_captured_pct > 0
                            ? ` · ${(bet.move_captured_pct * 100).toFixed(0)}% captured`
                            : ""}
                        </span>
                      )}
                    {bet.consensus_wallets >= 2 && (
                      <span
                        className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-700"
                        title="Distinct skilled wallets holding this same side"
                      >
                        {bet.consensus_wallets} skilled wallets
                      </span>
                    )}
                    {bet.consensus_contested && (
                      <span
                        className="rounded bg-orange-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-orange-700"
                        title="Skilled wallets hold BOTH sides of this market"
                      >
                        Contested
                      </span>
                    )}
                    {bet.conviction === "high" && (
                      <span
                        className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-indigo-700"
                        title={`This entry is ${bet.conviction_z.toFixed(1)} standard deviations above the wallet's usual BUY size`}
                      >
                        Sized up {bet.conviction_z > 0 ? `${bet.conviction_z.toFixed(1)}σ` : ""}
                      </span>
                    )}
                    {bet.category && (
                      <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-500">
                        {bet.category}
                      </span>
                    )}
                    <span className="font-mono text-[11px] text-zinc-400">
                      {formatRelativeSeconds(bet.bought_at)}
                    </span>
                  </div>

                  <p className="mt-1.5 text-sm font-semibold leading-snug text-zinc-900">
                    {bet.title || bet.slug}
                  </p>
                </div>

                <div className="shrink-0 text-right">
                  <p className="font-mono text-2xl font-semibold tabular-nums text-zinc-900">
                    {usdFormatter.format(bet.current_position_value_usdc)}
                  </p>
                  <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                    Position
                  </p>
                </div>
              </div>

              {bet.tradability_reasons.length > 0 && (
                <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
                  {bet.tradability_reasons.join(" · ")}
                </div>
              )}

              <div className="mt-3 grid grid-cols-3 gap-3 rounded-md bg-zinc-50 px-3 py-2">
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                    Entry
                  </p>
                  <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                    ${bet.entry_price.toFixed(3)}
                  </p>
                  <p className="font-mono text-[10px] text-zinc-500">
                    {shareFormatter.format(bet.entry_size)} sh ·{" "}
                    {usdFormatter.format(bet.entry_usdc_size)}
                  </p>
                </div>
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                    Now
                  </p>
                  <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                    {bet.current_outcome_price > 0
                      ? `$${bet.current_outcome_price.toFixed(3)}`
                      : "—"}
                  </p>
                  <p className={`font-mono text-[10px] ${driftColor(drift)}`}>
                    {drift !== null
                      ? `${drift > 0 ? "+" : ""}${drift.toFixed(1)}% vs entry`
                      : "no live price"}
                  </p>
                </div>
                <div className="text-right">
                  <p className="text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                    Signal age
                  </p>
                  <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                    {bet.signal_age_days}d
                  </p>
                  <p className="font-mono text-[10px] text-zinc-500">
                    latest BUY
                  </p>
                </div>
              </div>

              {(showPolyCta || showKalshiCta) && (
                <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                  {showPolyCta && (
                    <a
                      className="inline-flex flex-1 items-center justify-center rounded-md bg-emerald-600 px-3 py-2 text-sm font-semibold text-white hover:bg-emerald-700"
                      href={bet.polymarket_market_url}
                      rel="noopener noreferrer"
                      target="_blank"
                    >
                      Tail on Polymarket
                    </a>
                  )}
                  {showKalshiCta && (
                    <a
                      className="inline-flex flex-1 flex-col items-center justify-center rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm font-semibold text-blue-900 hover:bg-blue-100"
                      href={bet.kalshi_market_url}
                      rel="noopener noreferrer"
                      target="_blank"
                      title={bet.kalshi_title || bet.kalshi_ticker}
                    >
                      <span>Tail on Kalshi</span>
                      <span className="mt-0.5 font-mono text-[10px] font-normal text-blue-800">
                        {bet.kalshi_match_confidence > 0
                          ? `${(bet.kalshi_match_confidence * 100).toFixed(0)}% match · `
                          : ""}
                        {bet.kalshi_yes_price > 0
                          ? `$${bet.kalshi_yes_price.toFixed(3)} YES`
                          : "price unknown"}
                        {bet.kalshi_vs_entry_cents !== 0
                          ? ` · ${bet.kalshi_vs_entry_cents > 0 ? "+" : ""}${bet.kalshi_vs_entry_cents.toFixed(1)}¢ vs wallet entry`
                          : ""}
                      </span>
                    </a>
                  )}
                </div>
              )}

              <div className="mt-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-zinc-400">Signal source:</span>
                  {bet.polymarket_profile_url ? (
                    <a
                      className="font-semibold text-zinc-700 hover:text-zinc-900 hover:underline"
                      href={bet.polymarket_profile_url}
                      rel="noopener noreferrer"
                      target="_blank"
                      title="Open wallet on Polymarket"
                    >
                      {bet.wallet_name || shortWallet(bet.proxy_wallet)}
                    </a>
                  ) : (
                    <span className="font-semibold text-zinc-700">
                      {bet.wallet_name || shortWallet(bet.proxy_wallet)}
                    </span>
                  )}
                  <span className="font-mono text-zinc-400">
                    ({shortWallet(bet.proxy_wallet)})
                  </span>
                  <Link
                    className="font-semibold text-emerald-700 hover:text-emerald-900 hover:underline"
                    href={`/wallet/${bet.proxy_wallet}`}
                    title="Open wallet detail"
                  >
                    details
                  </Link>
                  <span className="text-zinc-300">·</span>
                  <span>
                    Forecast confidence{" "}
                    <span className="font-mono font-semibold text-zinc-700">
                      {(bet.skill_likelihood * 100).toFixed(1)}%
                    </span>
                  </span>
                  <span className="text-zinc-300">·</span>
                  <span>
                    <span className="font-mono font-semibold text-zinc-700">
                      {bet.independent_settled_events.toFixed(1)}
                    </span>{" "}
                    independent events
                  </span>
                </div>
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
