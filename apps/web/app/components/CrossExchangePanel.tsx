import type { ReactElement } from "react";

export type CrossExchangeSignal = {
  proxy_wallet: string;
  wallet_name: string;
  skill_likelihood: number;
  resolved_trades: number;

  polymarket_condition_id: string;
  polymarket_slug: string;
  polymarket_event_slug: string;
  polymarket_title: string;
  polymarket_outcome_index: number;
  polymarket_outcome: string;
  polymarket_yes_price: number;
  polymarket_position_size: number;
  polymarket_position_value_usdc: number;

  kalshi_ticker: string;
  kalshi_event_ticker: string;
  kalshi_title: string;
  kalshi_yes_price: number;

  price_dislocation: number;
  price_dislocation_pct: number;
  cheaper_exchange: "kalshi" | "polymarket" | string;
  recommended_action: string;

  match_confidence: number;
  match_status: string;

  opportunity_score: number;
  observed_at: string;

  polymarket_profile_url: string;
  polymarket_market_url: string;
  kalshi_market_url: string;
};

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "";
  const diff = Math.round((Date.now() - ts) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function priceCell(price: number): string {
  return price.toFixed(3);
}

function shortWallet(addr: string): string {
  if (addr.length < 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function ExchangeChip({ label, isCheaper }: { label: string; isCheaper: boolean }): ReactElement {
  return (
    <span
      className={[
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        isCheaper
          ? "bg-emerald-50 text-emerald-700"
          : "bg-zinc-100 text-zinc-500",
      ].join(" ")}
    >
      {label}
    </span>
  );
}

export default function CrossExchangePanel({
  signals,
  generatedAt,
}: {
  signals: CrossExchangeSignal[];
  generatedAt: string | null;
}): ReactElement {
  return (
    <section className="overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="flex items-baseline justify-between border-b border-zinc-200 px-5 py-4">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-widest text-zinc-400">
            Cross-Exchange Dislocations
          </h2>
          <p className="mt-0.5 text-sm text-zinc-600">
            Skilled Polymarket wallets&apos; positions vs. Kalshi prices
          </p>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-zinc-400">
          {signals.length > 0 && (
            <span className="font-mono">{signals.length} signal{signals.length === 1 ? "" : "s"}</span>
          )}
          {generatedAt && (
            <span className="font-mono">refreshed {formatRelative(generatedAt)}</span>
          )}
        </div>
      </div>

      {signals.length === 0 ? (
        <div className="px-5 py-10 text-center">
          <p className="text-sm font-semibold text-zinc-900">No dislocations active</p>
          <p className="mt-1.5 text-xs leading-relaxed text-zinc-500">
            Either no skilled Polymarket wallets are holding linked positions, or all current
            spreads are below threshold. Run the Polymarket ingestor and seed market_links to
            populate this view.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-zinc-100">
          {signals.map((sig, index) => {
            const kalshiCheaper = sig.cheaper_exchange === "kalshi";
            const dirArrow = kalshiCheaper ? "←" : "→";

            return (
              <li
                className="px-5 py-4 hover:bg-zinc-50"
                key={`${sig.proxy_wallet}-${sig.polymarket_condition_id}-${sig.kalshi_ticker}`}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-zinc-400">#{index + 1}</span>
                      <span
                        className={[
                          "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase",
                          sig.match_status === "approved"
                            ? "bg-emerald-50 text-emerald-700"
                            : "bg-amber-50 text-amber-700",
                        ].join(" ")}
                      >
                        {sig.match_status}
                      </span>
                      <span className="font-mono text-[10px] text-zinc-500">
                        match {(sig.match_confidence * 100).toFixed(0)}%
                      </span>
                    </div>
                    {sig.polymarket_market_url ? (
                      <a
                        className="mt-1.5 block text-sm font-semibold leading-snug text-zinc-900 hover:underline"
                        href={sig.polymarket_market_url}
                        rel="noopener noreferrer"
                        target="_blank"
                        title="Open this market on Polymarket"
                      >
                        {sig.polymarket_title || sig.kalshi_title || sig.polymarket_slug}
                      </a>
                    ) : (
                      <p className="mt-1.5 text-sm font-semibold leading-snug text-zinc-900">
                        {sig.polymarket_title || sig.kalshi_title || sig.polymarket_slug}
                      </p>
                    )}
                    <p className="mt-1 text-xs leading-relaxed text-zinc-500">
                      {sig.recommended_action}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="font-mono text-2xl font-semibold tabular-nums text-zinc-900">
                      {sig.price_dislocation_pct.toFixed(1)}
                      <span className="text-base text-zinc-400">pp</span>
                    </p>
                    <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
                      Spread
                    </p>
                  </div>
                </div>

                {/* Price comparison row */}
                <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-3 rounded-md bg-zinc-50 px-3 py-2">
                  <div>
                    <div className="flex items-center gap-1.5">
                      <ExchangeChip label="Polymarket" isCheaper={!kalshiCheaper} />
                      <span className="font-mono text-xs text-zinc-500">
                        {sig.polymarket_outcome || (sig.polymarket_outcome_index === 0 ? "YES" : "NO")}
                      </span>
                    </div>
                    <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                      ${priceCell(sig.polymarket_yes_price)}
                    </p>
                  </div>
                  <div className="font-mono text-lg text-zinc-400">{dirArrow}</div>
                  <div className="text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      {sig.kalshi_market_url ? (
                        <a
                          className="font-mono text-xs text-zinc-500 hover:text-zinc-900 hover:underline"
                          href={sig.kalshi_market_url}
                          rel="noopener noreferrer"
                          target="_blank"
                          title="Open this market on Kalshi"
                        >
                          {sig.kalshi_ticker}
                        </a>
                      ) : (
                        <span className="font-mono text-xs text-zinc-500">{sig.kalshi_ticker}</span>
                      )}
                      <ExchangeChip label="Kalshi" isCheaper={kalshiCheaper} />
                    </div>
                    <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-zinc-900">
                      ${priceCell(sig.kalshi_yes_price)}
                    </p>
                  </div>
                </div>

                {/* Wallet provenance row */}
                <div className="mt-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
                  <div className="flex items-center gap-2">
                    <span className="text-zinc-400">Signal source:</span>
                    {sig.polymarket_profile_url ? (
                      <a
                        className="font-semibold text-zinc-700 hover:text-zinc-900 hover:underline"
                        href={sig.polymarket_profile_url}
                        rel="noopener noreferrer"
                        target="_blank"
                        title="Open wallet on Polymarket"
                      >
                        {sig.wallet_name || shortWallet(sig.proxy_wallet)}
                      </a>
                    ) : (
                      <span className="font-semibold text-zinc-700">
                        {sig.wallet_name || shortWallet(sig.proxy_wallet)}
                      </span>
                    )}
                    <span className="font-mono text-zinc-400">
                      ({shortWallet(sig.proxy_wallet)})
                    </span>
                    <span className="text-zinc-300">·</span>
                    <span>
                      skill <span className="font-mono font-semibold text-zinc-700">
                        {(sig.skill_likelihood * 100).toFixed(1)}%
                      </span>
                    </span>
                    <span className="text-zinc-300">·</span>
                    <span>
                      <span className="font-mono font-semibold text-zinc-700">{sig.resolved_trades}</span> resolved
                    </span>
                  </div>
                  <div className="font-mono">
                    {usdFormatter.format(sig.polymarket_position_value_usdc)} held
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
