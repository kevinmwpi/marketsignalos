import { useEffect, useMemo, useState } from "react";
import {
  loadResearchSnapshot,
  snapshotUrl,
  type ResearchPosition,
  type ResearchSnapshot,
  type ResearchTrade,
  type ResearchWallet,
} from "@/lib/research-snapshot";

const money = (value: number | null) =>
  value === null
    ? "Unavailable"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      }).format(value);
const cents = (value: number | null) =>
  value === null ? "Unavailable" : `${(100 * value).toFixed(1)}¢`;
const date = (value: string | number) =>
  new Date(typeof value === "number" ? value * 1000 : value).toLocaleString(
    undefined,
    { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" },
  ) + " UTC";
const muted = "text-[hsl(var(--muted-foreground))]";

function Market({ row }: { row: ResearchTrade | ResearchPosition }) {
  return (
    <div className="min-w-0">
      {row.event_slug ? (
        <a
          href={`https://polymarket.com/event/${row.event_slug}`}
          target="_blank"
          rel="noopener noreferrer"
          className="break-words font-medium underline decoration-dotted underline-offset-4 hover:text-[hsl(var(--primary))]"
        >
          {row.title}
        </a>
      ) : (
        <span className="break-words font-medium">{row.title}</span>
      )}
      <span className={`mt-1 block text-xs ${muted}`}>
        Outcome: {row.outcome}
      </span>
    </div>
  );
}

function Sample({
  wallet,
  kind,
}: {
  wallet: ResearchWallet;
  kind: "trades" | "positions";
}) {
  const [showAll, setShowAll] = useState(false);
  const sample = wallet[kind];
  const rows = showAll ? sample.rows : sample.rows.slice(0, 5);
  return (
    <section
      className="min-w-0 space-y-3"
      aria-label={`${kind} for ${wallet.name}`}
    >
      <div>
        <h3 className="text-sm font-semibold">
          {kind === "positions" ? "Holdings at capture" : "Recent trade sample"}
        </h3>
        <p className={`mt-1 text-xs ${muted}`}>
          Observed {date(sample.observed_at)}.
        </p>
        <p className={`mt-1 text-xs ${muted}`}>
          {sample.status === "unavailable"
            ? "Source request failed or was skipped. Holdings and activity are unknown."
            : `${sample.rows.length} usable rows returned${sample.possibly_truncated ? "; sample limit reached, more may exist" : "; source-filtered sample"}.`}
          {sample.rows_rejected > 0 &&
            ` ${sample.rows_rejected} invalid or duplicate position rows excluded.`}
        </p>
      </div>
      {sample.status === "ok" && rows.length === 0 && (
        <p className={`text-sm ${muted}`}>
          No usable rows in this sample. This does not establish an empty
          portfolio or no trading history.
        </p>
      )}
      <ul className="divide-y divide-[hsl(var(--border))]">
        {rows.map((row, index) => (
          <li key={`${row.asset}-${index}`} className="space-y-2 py-3 text-sm">
            <Market row={row} />
            {"side" in row ? (
              <>
                <p className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
                  <span>
                    {row.side} · {cents(row.price)}
                  </span>
                  <span>Reported amount {money(row.notional)}</span>
                </p>
                <p className={`text-xs ${muted}`}>
                  {date(row.timestamp)}
                  {row.transaction_hash && (
                    <>
                      {" "}
                      ·{" "}
                      <a
                        className="underline"
                        href={`https://polygonscan.com/tx/${row.transaction_hash}`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Transaction ↗
                      </a>
                    </>
                  )}
                </p>
              </>
            ) : (
              <p className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
                <span>
                  {row.size.toLocaleString(undefined, {
                    maximumFractionDigits: 2,
                  })}{" "}
                  shares
                </span>
                <span>Average entry {cents(row.average_price)}</span>
                <span>Reported mark {cents(row.reported_price)}</span>
                <span>Reported value {money(row.reported_value)}</span>
              </p>
            )}
          </li>
        ))}
      </ul>
      {sample.rows.length > 5 && (
        <button
          type="button"
          className="rounded border border-[hsl(var(--border))] px-3 py-2 text-xs hover:bg-[hsl(var(--secondary))]"
          onClick={() => setShowAll(!showAll)}
        >
          {showAll
            ? "Show fewer"
            : `Show all ${sample.rows.length} sampled ${kind}`}
        </button>
      )}
    </section>
  );
}

export default function ResearchCandidatesPanel() {
  const [snapshot, setSnapshot] = useState<ResearchSnapshot | null>(null);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState("");
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    let active = true;
    setError(false);
    void loadResearchSnapshot(controller.signal)
      .then((data) => {
        if (active) setSnapshot(data);
      })
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => window.clearTimeout(timeout));
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [revision]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const wallets = useMemo(
    () =>
      snapshot?.wallets.filter((wallet) =>
        `${wallet.name} ${wallet.address} ${wallet.positions.rows.map((row) => row.title).join(" ")} ${wallet.trades.rows.map((row) => row.title).join(" ")}`
          .toLowerCase()
          .includes(search.toLowerCase()),
      ) ?? [],
    [snapshot, search],
  );
  const stale = snapshot && now - Date.parse(snapshot.started_at) > 86_400_000;

  return (
    <section
      className="space-y-4"
      aria-labelledby="research-title"
      data-testid="research-candidates"
    >
      <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="research-title" className="text-lg font-semibold">
            Wallets to research
          </h2>
          <span className="rounded-full border border-[hsl(var(--border))] px-3 py-1 font-mono text-[10px] uppercase">
            Dated snapshot · skill not evaluated
          </span>
        </div>
        <p className={`mt-2 max-w-4xl text-sm leading-relaxed ${muted}`}>
          A small sample selected from Polymarket’s daily volume leaderboard.
          High volume is not evidence of forecasting skill. These accounts have
          not been qualified for the actionable feed.
        </p>
        {error && (
          <div role="alert" className="mt-4 text-sm">
            Could not load or validate the research snapshot.
            {snapshot && " Showing the previously loaded capture."}
            <button
              type="button"
              className="ml-3 underline"
              onClick={() => setRevision(revision + 1)}
            >
              Retry snapshot
            </button>
          </div>
        )}
        {!snapshot && !error && (
          <p role="status" className="mt-4 text-sm">
            Loading wallet observations…
          </p>
        )}
        {snapshot && (
          <>
            <p className="mt-4 text-sm" data-testid="snapshot-status">
              <strong>{snapshot.wallets.length} wallets</strong> · Captured{" "}
              {date(snapshot.generated_at)}
              {snapshot.status === "partial" &&
                " · Some requests or records were unavailable"}
            </p>
            <p className={`mt-2 text-xs leading-relaxed ${muted}`}>
              Up to 100 trades per wallet from{" "}
              {date(snapshot.trade_window.start)} to{" "}
              {date(snapshot.trade_window.end)}, plus up to 50 non-redeemable
              holdings ordered by reported value. Holdings were fetched
              separately and are not simultaneous. Positions may have changed;
              market availability and executable prices are unverified.
            </p>
            <p className={`mt-2 text-xs ${muted}`}>
              Refresh requires a new collection and publication. Reloading this
              page does not run ingestion.{" "}
              <a
                className="underline"
                href={snapshotUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Download data and request receipts ↗
              </a>
            </p>
            {stale && (
              <p
                role="status"
                className="mt-3 rounded border border-amber-400/40 bg-amber-400/10 p-3 text-sm"
              >
                This capture is over 24 hours old. Treat it as historical
                observations.
              </p>
            )}
            <label
              className="mt-4 block text-xs font-medium"
              htmlFor="research-search"
            >
              Find a wallet or market
            </label>
            <input
              id="research-search"
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Name, wallet address, or market title"
              className="mt-2 w-full rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm sm:max-w-lg"
            />
          </>
        )}
      </div>
      {wallets.map((wallet) => (
        <details
          key={wallet.address}
          className="group rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]"
          data-testid="research-wallet"
        >
          <summary className="cursor-pointer p-4 marker:text-[hsl(var(--muted-foreground))]">
            <span className="ml-2 font-semibold break-words">
              {wallet.name}
            </span>
            <span className={`ml-3 font-mono text-xs ${muted}`}>
              {wallet.address.slice(0, 8)}…{wallet.address.slice(-6)}
            </span>
            <span className={`mt-2 block text-xs ${muted}`}>
              {wallet.positions.status === "ok"
                ? `${wallet.positions.rows.length} sampled holdings`
                : "Holdings unavailable"}{" "}
              ·{" "}
              {wallet.trades.status === "ok"
                ? `${wallet.trades.rows.length} sampled trades`
                : "Trades unavailable"}{" "}
              · Expand to inspect
            </span>
          </summary>
          <div className="space-y-5 border-t border-[hsl(var(--border))] p-4 sm:p-5">
            <p className="break-all font-mono text-xs">
              <a
                className="underline"
                href={`https://polymarket.com/profile/${wallet.address}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {wallet.address} · Polymarket profile ↗
              </a>
            </p>
            <div className="grid gap-6 lg:grid-cols-2">
              <Sample wallet={wallet} kind="positions" />
              <Sample wallet={wallet} kind="trades" />
            </div>
          </div>
        </details>
      ))}
      {snapshot && wallets.length === 0 && (
        <p role="status" className={`p-4 text-sm ${muted}`}>
          No sampled wallets match this search.
        </p>
      )}
    </section>
  );
}
