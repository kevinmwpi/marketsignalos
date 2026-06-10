import type { ReactElement } from "react";

export type ExitSignal = {
  proxy_wallet: string;
  wallet_name: string;
  skill_likelihood: number;

  condition_id: string;
  outcome_index: number;
  outcome: string;
  title: string;
  event_slug: string;

  exit_type: string; // "closed" | "trimmed"
  previous_size: number;
  current_size: number;
  reduction_pct: number;
  previous_value_usdc: number;
  current_outcome_price: number;

  from_snapshot_at: string;
  to_snapshot_at: string;

  was_surfaced: boolean;
  surfaced_recorded_at: string;

  polymarket_market_url: string;
  polymarket_profile_url: string;
  detected_at: string;
};

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

function shortWallet(addr: string): string {
  if (addr.length < 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function formatRelativeIso(iso: string): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const diff = Math.round((Date.now() - ts) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

export default function ExitSignalsPanel({
  exits,
}: {
  exits: ExitSignal[];
}): ReactElement | null {
  if (exits.length === 0) return null;

  return (
    <div className="rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-zinc-900">Exit signals</h2>
        <p className="mt-0.5 text-[11px] leading-relaxed text-zinc-500">
          Skilled wallets selling out of still-open markets. If you tailed
          these, consider whether the thesis still holds.
        </p>
      </div>
      <ul className="divide-y divide-zinc-100">
        {exits.map((exit) => (
          <li
            className="px-4 py-3"
            key={`${exit.proxy_wallet}-${exit.condition_id}-${exit.outcome_index}-${exit.to_snapshot_at}`}
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <span
                className={[
                  "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                  exit.exit_type === "closed"
                    ? "bg-red-50 text-red-700"
                    : "bg-amber-50 text-amber-800",
                ].join(" ")}
              >
                {exit.exit_type === "closed"
                  ? "Closed position"
                  : `Trimmed ${(exit.reduction_pct * 100).toFixed(0)}%`}
              </span>
              {exit.was_surfaced && (
                <span
                  className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-700"
                  title="This position previously appeared in the skilled-bets feed"
                >
                  Was surfaced
                </span>
              )}
              <span className="font-mono text-[10px] text-zinc-400">
                {formatRelativeIso(exit.detected_at)}
              </span>
            </div>

            <p className="mt-1.5 text-xs font-semibold leading-snug text-zinc-900">
              {exit.polymarket_market_url ? (
                <a
                  className="hover:underline"
                  href={exit.polymarket_market_url}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  {exit.title || exit.event_slug}
                </a>
              ) : (
                exit.title || exit.event_slug
              )}
            </p>

            <p className="mt-1 text-[11px] text-zinc-500">
              <a
                className="font-semibold text-zinc-700 hover:text-zinc-900 hover:underline"
                href={exit.polymarket_profile_url}
                rel="noopener noreferrer"
                target="_blank"
              >
                {exit.wallet_name || shortWallet(exit.proxy_wallet)}
              </a>{" "}
              sold {exit.outcome || (exit.outcome_index === 0 ? "YES" : "NO")} ·{" "}
              {usdFormatter.format(exit.previous_value_usdc)} position
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}
