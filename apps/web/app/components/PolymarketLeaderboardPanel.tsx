import type { ReactElement } from "react";

export type PolymarketWalletSkill = {
  proxy_wallet: string;
  name: string;
  pseudonym: string;
  resolved_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  skill_likelihood: number;
  stddevs_above_expected: number;
  total_volume_usdc: number;
  total_pnl_usdc: number;
  avg_position_size_usdc: number;
  trade_count: number;
  last_activity_at: number;
  computed_at: string;
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
            Polymarket Skilled Wallets
          </h2>
          {computedAt && (
            <span className="font-mono text-[10px] text-zinc-400">
              {formatRelativeIso(computedAt)}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-zinc-500">On-chain win rate vs. random</p>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left">
          <thead>
            <tr className="border-b border-zinc-100">
              <th className="px-4 py-2 text-left text-xs font-medium text-zinc-400">Wallet</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Skill</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">W/L</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">PnL</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-zinc-400">Active</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.length === 0 ? (
              <tr>
                <td className="px-4 py-4 text-xs text-zinc-400" colSpan={5}>
                  No qualifying wallets yet. Run the Polymarket ingestor to populate.
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr className="hover:bg-zinc-50" key={row.proxy_wallet}>
                  <td className="px-4 py-2.5">
                    <span className="block font-mono text-xs font-medium text-zinc-900">
                      {row.name || shortWallet(row.proxy_wallet)}
                    </span>
                    {row.name && (
                      <span className="block font-mono text-[10px] text-zinc-400">
                        {shortWallet(row.proxy_wallet)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {formatPct(row.skill_likelihood)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-zinc-700">
                    {numberFormatter.format(row.wins)}/{numberFormatter.format(row.losses)}
                  </td>
                  <td
                    className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${pnlClass(row.total_pnl_usdc)}`}
                  >
                    {row.total_pnl_usdc >= 0 ? "+" : ""}
                    {compactUsd.format(row.total_pnl_usdc)}
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
