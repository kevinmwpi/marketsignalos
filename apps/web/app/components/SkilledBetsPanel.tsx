import type { ReactElement } from "react";

export type SkilledBet = {
  proxy_wallet: string;
  wallet_name: string;
  skill_likelihood: number;
  resolved_trades: number;
  win_rate: number;

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

  current_position_size: number;
  current_position_value_usdc: number;
  current_market_yes_price: number;

  polymarket_profile_url: string;
  polymarket_market_url: string;

  // Kalshi mirror — empty/zero when no equivalent Kalshi market was matched.
  kalshi_ticker: string;
  kalshi_event_ticker: string;
  kalshi_title: string;
  kalshi_market_url: string;
  kalshi_yes_price: number;
  kalshi_match_confidence: number;
  kalshi_match_status: string;
};

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const shareFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

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

export default function SkilledBetsPanel({
  bets,
}: {
  bets: SkilledBet[];
}): ReactElement {
  if (bets.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-white p-10 text-center">
        <p className="text-sm font-semibold text-zinc-900">No active skilled bets yet</p>
        <p className="mt-1.5 text-xs leading-relaxed text-zinc-500">
          No skilled wallets are holding traceable open positions yet. Click{" "}
          <span className="font-mono">Run ingest</span> above to scan Polymarket
          leaderboards and match wallet positions against live Kalshi markets,
          or lower the skill threshold.
        </p>
      </div>
    );
  }

  return (
    <ul className="space-y-3">
      {bets.map((bet, index) => {
        const drift = driftPct(bet.entry_price, bet.current_market_yes_price);
        const outcomeLabel =
          bet.outcome || (bet.outcome_index === 0 ? "YES" : "NO");

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
                    {bet.category && (
                      <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-500">
                        {bet.category}
                      </span>
                    )}
                    <span className="font-mono text-[11px] text-zinc-400">
                      {formatRelativeSeconds(bet.bought_at)}
                    </span>
                  </div>

                  {bet.polymarket_market_url ? (
                    <a
                      className="mt-1.5 block text-sm font-semibold leading-snug text-zinc-900 hover:underline"
                      href={bet.polymarket_market_url}
                      rel="noopener noreferrer"
                      target="_blank"
                      title="Open this market on Polymarket"
                    >
                      {bet.title || bet.slug}
                    </a>
                  ) : (
                    <p className="mt-1.5 text-sm font-semibold leading-snug text-zinc-900">
                      {bet.title || bet.slug}
                    </p>
                  )}
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

              {/* Price + drift */}
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
                    {bet.current_market_yes_price > 0
                      ? `$${bet.current_market_yes_price.toFixed(3)}`
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
                    Still held
                  </p>
                  <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                    {shareFormatter.format(bet.current_position_size)} sh
                  </p>
                  <p className="font-mono text-[10px] text-zinc-500">
                    {usdFormatter.format(bet.current_position_value_usdc)} value
                  </p>
                </div>
              </div>

              {/* Kalshi mirror — the central "tail this on Kalshi" CTA. */}
              {bet.kalshi_ticker && bet.kalshi_market_url && (
                <div className="mt-3 flex items-start justify-between gap-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-semibold uppercase tracking-widest text-blue-700">
                        Mirror on Kalshi
                      </span>
                      <span
                        className={[
                          "rounded px-1 py-px text-[9px] font-semibold uppercase tracking-wide",
                          bet.kalshi_match_status === "approved"
                            ? "bg-blue-200 text-blue-900"
                            : "bg-amber-100 text-amber-800",
                        ].join(" ")}
                      >
                        {bet.kalshi_match_status || "match"}
                      </span>
                      <span className="font-mono text-[10px] text-blue-700/70">
                        {(bet.kalshi_match_confidence * 100).toFixed(0)}% conf
                      </span>
                    </div>
                    <a
                      className="mt-1 block truncate text-xs font-semibold text-blue-900 hover:underline"
                      href={bet.kalshi_market_url}
                      rel="noopener noreferrer"
                      target="_blank"
                      title="Open the matching Kalshi event"
                    >
                      {bet.kalshi_title || bet.kalshi_ticker}
                    </a>
                    <p className="mt-0.5 font-mono text-[10px] text-blue-700/70">
                      {bet.kalshi_ticker}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="text-[10px] font-semibold uppercase tracking-widest text-blue-700/70">
                      Kalshi YES
                    </p>
                    <p className="mt-0.5 font-mono text-sm font-semibold tabular-nums text-blue-900">
                      {bet.kalshi_yes_price > 0
                        ? `$${bet.kalshi_yes_price.toFixed(3)}`
                        : "—"}
                    </p>
                    {bet.kalshi_yes_price > 0 && bet.current_market_yes_price > 0 && (
                      <p className="font-mono text-[10px] text-blue-700/70">
                        {bet.kalshi_yes_price > bet.current_market_yes_price
                          ? `+${((bet.kalshi_yes_price - bet.current_market_yes_price) * 100).toFixed(1)}¢`
                          : `${((bet.kalshi_yes_price - bet.current_market_yes_price) * 100).toFixed(1)}¢`}{" "}
                        vs Poly
                      </p>
                    )}
                  </div>
                </div>
              )}

              {/* Wallet provenance */}
              <div className="mt-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
                <div className="flex items-center gap-2">
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
                  <span className="text-zinc-300">·</span>
                  <span>
                    skill{" "}
                    <span className="font-mono font-semibold text-zinc-700">
                      {(bet.skill_likelihood * 100).toFixed(1)}%
                    </span>
                  </span>
                  <span className="text-zinc-300">·</span>
                  <span>
                    <span className="font-mono font-semibold text-zinc-700">
                      {(bet.win_rate * 100).toFixed(0)}%
                    </span>{" "}
                    on{" "}
                    <span className="font-mono font-semibold text-zinc-700">
                      {bet.resolved_trades}
                    </span>{" "}
                    resolved
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
