import { z } from "zod";

const amount = z.number().finite().nonnegative().max(1e15);
const price = amount.max(1).nullable();
const timestamp = z.string().datetime({ offset: true });
const market = z.object({
  condition_id: z.string().regex(/^0x[0-9a-f]{64}$/),
  asset: z.string().regex(/^[0-9]{1,100}$/),
  title: z.string().max(300),
  outcome: z.string().max(300),
  event_slug: z
    .string()
    .regex(/^[a-zA-Z0-9_-]{1,250}$/)
    .nullable(),
  size: amount.positive(),
});
const trade = market.extend({
  timestamp: z.number().int().positive(),
  side: z.enum(["BUY", "SELL"]),
  price: amount.max(1),
  notional: amount.nullable(),
  transaction_hash: z
    .string()
    .regex(/^0x[0-9a-fA-F]{64}$/)
    .nullable(),
});
const position = market.extend({
  average_price: price,
  reported_price: price,
  reported_value: amount.nullable(),
});
const coverage = z.object({
  status: z.enum(["ok", "unavailable"]),
  observed_at: timestamp,
  rows_returned: z.number().int().nonnegative(),
  rows_rejected: z.number().int().nonnegative(),
  possibly_truncated: z.boolean(),
});

export const researchSnapshotSchema = z.object({
  schema_version: z.literal(1),
  collector_version: z.literal("research-snapshot-v1"),
  started_at: timestamp,
  generated_at: timestamp,
  source: z.literal("https://data-api.polymarket.com"),
  selection: z.literal("OVERALL / DAY / VOL, first page"),
  status: z.enum(["complete", "partial"]),
  trade_window: z.object({
    start: z.number().int().positive(),
    end: z.number().int().positive(),
  }),
  wallets: z
    .array(
      z.object({
        address: z.string().regex(/^0x[0-9a-f]{40}$/),
        name: z.string().max(300),
        evaluation_status: z.literal("not_evaluated"),
        trades: coverage.extend({ rows: z.array(trade).max(100) }),
        positions: coverage.extend({ rows: z.array(position).max(50) }),
      }),
    )
    .min(1)
    .max(20),
});

export type ResearchSnapshot = z.infer<typeof researchSnapshotSchema>;
export type ResearchWallet = ResearchSnapshot["wallets"][number];
export type ResearchTrade = z.infer<typeof trade>;
export type ResearchPosition = z.infer<typeof position>;
export const snapshotUrl = `${import.meta.env.BASE_URL}data/research-snapshot.json`;

export async function loadResearchSnapshot(
  signal: AbortSignal,
): Promise<ResearchSnapshot> {
  const response = await fetch(snapshotUrl, { cache: "no-store", signal });
  if (!response.ok)
    throw new Error(`Snapshot unavailable (${response.status})`);
  const result = researchSnapshotSchema.parse(await response.json());
  if (Date.parse(result.generated_at) > Date.now() + 300_000) {
    throw new Error("Snapshot capture time is in the future");
  }
  return result;
}
