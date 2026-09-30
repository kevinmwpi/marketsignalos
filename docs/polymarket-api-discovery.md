# Polymarket API discovery report

Wallet probed: `0x56687bf447db6ffa42ffe2204a05edaa20f55839` (Theo4 — top all-time profit at ~$22M)

Probes: 16

Returning 200 + JSON: 13/16

## Synthesis — what to build the ingestor on

**Auth**: all read endpoints work unauthenticated. No keypair or API key needed.

**Leaderboard** (`lb-api.polymarket.com`):
- `GET /profit?window=all&limit=N` returns `[{proxyWallet, amount, pseudonym, name, bio, profileImage}]`
- `window=week` returns `400 invalid request` — need to discover the supported window values (try `1d`, `1w`, `1m`, `7d`); for now `window=all` is the working seed
- TODO: try `GET /volume?window=all` for the volume-ranked leaderboard
- This replaces the planned `polymarket.com/leaderboard` Playwright scrape — pure JSON endpoint, no browser needed

**Per-wallet activity** (`data-api.polymarket.com`):
- `GET /activity?user=<proxyWallet>&limit=N` → goldmine. Each row: `{type, conditionId, outcomeIndex, side, size, price, usdcSize, timestamp, transactionHash, slug, title, eventSlug, outcome}`. `type` is `TRADE` or `REDEEM` (REDEEM = settled). Pagination via `offset` (untested) or `limit`.
- `GET /trades?user=<proxyWallet>` → subset of activity, trades only
- `GET /positions?user=<proxyWallet>` → currently open positions (Theo4 returned `[]` — flat)
- `GET /value?user=<proxyWallet>` → `[{user, value}]` — current portfolio USD value

**Market metadata** (`gamma-api.polymarket.com`):
- `GET /markets?active=true&closed=false&limit=N` → open markets
- `GET /markets?closed=true&limit=N` → resolved markets, with `outcomePrices` indicating winners (e.g. `["1","0"]` = YES won)
- `GET /events?active=true&limit=N` → event-level groupings with nested markets
- Key fields: `id, conditionId, question, slug, endDate, category, outcomes, outcomePrices, closed, volumeNum, liquidity, lastTradePrice, bestBid, bestAsk`

**CLOB** (`clob.polymarket.com`):
- `GET /markets?limit=N` → market list with orderbook config (paginated via `next_cursor`)
- `GET /sampling-markets` → similar but only markets accepting orders
- Use for: current best bid/ask, tick size, min order size

**Goldsky subgraph**:
- URL: `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn`
- Entity confirmed: `orderFilledEvents { id maker taker makerAssetId takerAssetId makerAmountFilled takerAmountFilled fee timestamp transactionHash }`
- Useful for: historical backfill of trade events (Data API may truncate beyond N days), or cross-validation of Data API output

## Mapping to the design

| Plan layer | Source confirmed | Notes |
|---|---|---|
| Leaderboard scraper | `lb-api.polymarket.com/profit?window=all` | JSON endpoint, no Playwright needed |
| Per-wallet ingestion | `data-api.polymarket.com/activity?user=` | Single endpoint covers trades + redemptions |
| Market ingestion | `gamma-api.polymarket.com/markets` | Use `closed=true` to backfill resolved-market settlements |
| Skill scoring | Activity stream + resolved markets | Join on `conditionId`; REDEEM event marks settled trade |
| Polymarket → Kalshi matching | Gamma `question` + `category` + `endDate` | Match against Kalshi `event_ticker` / `subtitle` / `expiration_date` |

## Open questions (revisit before Phase 1)

1. **Volume leaderboard window values** — try `lb-api.polymarket.com/volume?window=all|day|week` to find what's accepted
2. **Activity pagination** — does `&offset=N` work? Or is there a cursor? Critical for backfilling whales with 10k+ trades
3. **Rate limits** — no documented limits encountered yet; informally cap at ~5 RPS per host to be polite
4. **Username stability** — `pseudonym` differed between `lb-api` (`Theo4`) and `data-api` (`Ironclad-Tenement`). The `name` field seems to be the canonical display name; treat `pseudonym` as ephemeral




## `gamma:markets`

- GET `https://gamma-api.polymarket.com/markets?limit=2&active=true&closed=false`
- status `200` · content-type `application/json` · bytes 12798
- list length: 2
- first row keys: `['id', 'question', 'conditionId', 'slug', 'resolutionSource', 'endDate', 'liquidity', 'startDate', 'image', 'icon', 'description', 'outcomes', 'outcomePrices', 'volume', 'active', 'closed', 'marketMakerAddress', 'createdAt', 'updatedAt', 'new', 'featured', 'submitted_by', 'archived', 'resolvedBy', 'restricted', 'groupItemTitle', 'groupItemThreshold', 'questionID', 'enableOrderBook', 'orderPriceMinTickSize', 'orderMinSize', 'volumeNum', 'liquidityNum', 'endDateIso', 'startDateIso', 'hasReviewedDates', 'volume24hr', 'volume1wk', 'volume1mo', 'volume1yr', 'clobTokenIds', 'umaBond', 'umaReward', 'volume24hrClob', 'volume1wkClob', 'volume1moClob', 'volume1yrClob', 'volumeClob', 'liquidityClob', 'makerBaseFee', 'takerBaseFee', 'customLiveness', 'acceptingOrders', 'negRisk', 'negRiskRequestID', 'events', 'ready', 'funded', 'acceptingOrdersTimestamp', 'cyom', 'competitive', 'pagerDutyNotificationEnabled', 'approved', 'rewardsMinSize', 'rewardsMaxSpread', 'spread', 'oneDayPriceChange', 'oneHourPriceChange', 'oneWeekPriceChange', 'oneMonthPriceChange', 'oneYearPriceChange', 'lastTradePrice', 'bestBid', 'bestAsk', 'automaticallyActive', 'clearBookOnStart', 'seriesColor', 'showGmpSeries', 'showGmpOutcome', 'manualActivation', 'negRiskOther', 'umaResolutionStatuses', 'pendingDeployment', 'deploying', 'deployingTimestamp', 'rfqEnabled', 'holdingRewardsEnabled', 'feesEnabled', 'requiresTranslation', 'feeType', 'feeSchedule']`
- shape:
```json
[
  {
    "id": "str:'540817'",
    "question": "str:'New Rihanna Album before GTA VI?'",
    "conditionId": "str(66)",
    "slug": "str:'new-rhianna-album-before-gta-vi-926'",
    "resolutionSource": "str:''",
    "endDate": "str:'2026-07-31T12:00:00Z'",
    "liquidity": "str:'44930.5642'",
    "startDate": "str:'2025-05-02T15:48:10.582Z'",
    "image": "str(100)",
    "icon": "str(100)",
    "description": "str(1222)",
    "outcomes": "str:'[\"Yes\", \"No\"]'",
    "outcomePrices": "str:'[\"0.65\", \"0.35\"]'",
    "volume": "str:'716676.6273730034'",
    "active": "bool",
    "closed": "bool",
    "marketMakerAddress": "str:''",
    "createdAt": "str:'2025-05-02T15:04:43.762151Z'",
    "updatedAt": "str:'2026-05-11T02:58:30.541847Z'",
    "new": "bool"
  }
]
```

## `gamma:events`

- GET `https://gamma-api.polymarket.com/events?limit=2&active=true&closed=false`
- status `200` · content-type `application/json` · bytes 35598
- list length: 2
- first row keys: `['id', 'ticker', 'slug', 'title', 'description', 'resolutionSource', 'startDate', 'creationDate', 'endDate', 'image', 'icon', 'active', 'closed', 'archived', 'new', 'featured', 'restricted', 'liquidity', 'volume', 'openInterest', 'createdAt', 'updatedAt', 'competitive', 'volume24hr', 'volume1wk', 'volume1mo', 'volume1yr', 'enableOrderBook', 'liquidityClob', 'negRisk', 'commentCount', 'markets', 'tags', 'cyom', 'showAllOutcomes', 'showMarketImages', 'enableNegRisk', 'automaticallyActive', 'gmpChartMode', 'negRiskAugmented', 'cumulativeMarkets', 'pendingDeployment', 'deploying', 'requiresTranslation', 'eventMetadata']`
- shape:
```json
[
  {
    "id": "str:'16167'",
    "ticker": "str:'microstrategy-sell-any-bitcoin-in-2025'",
    "slug": "str:'microstrategy-sell-any-bitcoin-in-2025'",
    "title": "str:'MicroStrategy sells any Bitcoin by ___ ?'",
    "description": "str(312)",
    "resolutionSource": "str:''",
    "startDate": "str:'2024-12-31T18:51:45.506005Z'",
    "creationDate": "str:'2024-12-31T18:51:45.506002Z'",
    "endDate": "str:'2025-12-31T12:00:00Z'",
    "image": "str(114)",
    "icon": "str(114)",
    "active": "bool",
    "closed": "bool",
    "archived": "bool",
    "new": "bool",
    "featured": "bool",
    "restricted": "bool",
    "liquidity": "float",
    "volume": "float",
    "openInterest": "float"
  }
]
```

## `gamma:markets_resolved`

- GET `https://gamma-api.polymarket.com/markets?limit=2&closed=true`
- status `200` · content-type `application/json` · bytes 9103
- list length: 2
- first row keys: `['id', 'question', 'conditionId', 'slug', 'twitterCardImage', 'endDate', 'category', 'liquidity', 'image', 'icon', 'description', 'outcomes', 'outcomePrices', 'volume', 'active', 'marketType', 'closed', 'marketMakerAddress', 'updatedBy', 'createdAt', 'updatedAt', 'closedTime', 'mailchimpTag', 'archived', 'restricted', 'volumeNum', 'liquidityNum', 'endDateIso', 'hasReviewedDates', 'readyForCron', 'volume24hr', 'volume1wk', 'volume1mo', 'volume1yr', 'clobTokenIds', 'fpmmLive', 'volume1wkAmm', 'volume1moAmm', 'volume1yrAmm', 'volume1wkClob', 'volume1moClob', 'volume1yrClob', 'events', 'creator', 'ready', 'funded', 'cyom', 'competitive', 'pagerDutyNotificationEnabled', 'approved', 'rewardsMinSize', 'rewardsMaxSpread', 'spread', 'oneDayPriceChange', 'oneHourPriceChange', 'oneWeekPriceChange', 'oneMonthPriceChange', 'oneYearPriceChange', 'lastTradePrice', 'bestBid', 'bestAsk', 'clearBookOnStart', 'manualActivation', 'negRiskOther', 'umaResolutionStatuses', 'pendingDeployment', 'deploying', 'rfqEnabled', 'holdingRewardsEnabled', 'feesEnabled', 'requiresTranslation', 'feeType']`
- shape:
```json
[
  {
    "id": "str:'12'",
    "question": "str(51)",
    "conditionId": "str(66)",
    "slug": "str(50)",
    "twitterCardImage": "str(80)",
    "endDate": "str:'2020-11-04T00:00:00Z'",
    "category": "str:'US-current-affairs'",
    "liquidity": "str:'0'",
    "image": "str(144)",
    "icon": "str(144)",
    "description": "str(542)",
    "outcomes": "str:'[\"Yes\", \"No\"]'",
    "outcomePrices": "str:'[\"0\", \"0\"]'",
    "volume": "str:'32257.445115'",
    "active": "bool",
    "marketType": "str:'normal'",
    "closed": "bool",
    "marketMakerAddress": "str(42)",
    "updatedBy": "int",
    "createdAt": "str:'2020-10-02T16:10:01.467Z'"
  }
]
```

## `clob:markets`

- GET `https://clob.polymarket.com/markets?limit=2`
- status `200` · content-type `application/json` · bytes 1823307
- top-level keys: `['data', 'next_cursor', 'limit', 'count']`
- shape:
```json
{
  "data": [
    {
      "enable_order_book": "bool",
      "active": "bool",
      "closed": "bool",
      "archived": "bool",
      "accepting_orders": "bool",
      "accepting_order_timestamp": "NoneType",
      "minimum_order_size": "int",
      "minimum_tick_size": "float",
      "condition_id": "str",
      "question_id": "str",
      "question": "str",
      "description": "str",
      "market_slug": "str",
      "end_date_iso": "str",
      "game_start_time": "str",
      "seconds_delay": "int",
      "fpmm": "str",
      "maker_base_fee": "int",
      "taker_base_fee": "int",
      "notifications_enabled": "bool"
    }
  ],
  "next_cursor": "str:'MTAwMA=='",
  "limit": "int",
  "count": "int"
}
```

## `clob:sampling-markets`

- GET `https://clob.polymarket.com/sampling-markets?limit=2`
- status `200` · content-type `application/json` · bytes 2450605
- top-level keys: `['data', 'next_cursor', 'limit', 'count']`
- shape:
```json
{
  "data": [
    {
      "enable_order_book": "bool",
      "active": "bool",
      "closed": "bool",
      "archived": "bool",
      "accepting_orders": "bool",
      "accepting_order_timestamp": "str",
      "minimum_order_size": "int",
      "minimum_tick_size": "float",
      "condition_id": "str",
      "question_id": "str",
      "question": "str",
      "description": "str",
      "market_slug": "str",
      "end_date_iso": "str",
      "game_start_time": "NoneType",
      "seconds_delay": "int",
      "fpmm": "str",
      "maker_base_fee": "int",
      "taker_base_fee": "int",
      "notifications_enabled": "bool"
    }
  ],
  "next_cursor": "str:'MTAwMA=='",
  "limit": "int",
  "count": "int"
}
```

## `lb:profit_week`

- GET `https://lb-api.polymarket.com/profit?window=week&limit=10`
- status `400` · content-type `application/json` · bytes 27
- top-level keys: `['error']`
- shape:
```json
{
  "error": "str:'invalid request'"
}
```

## `lb:volume_week`

- GET `https://lb-api.polymarket.com/volume?window=week&limit=10`
- status `400` · content-type `application/json` · bytes 27
- top-level keys: `['error']`
- shape:
```json
{
  "error": "str:'invalid request'"
}
```

## `lb:profit_all`

- GET `https://lb-api.polymarket.com/profit?window=all&limit=10`
- status `200` · content-type `application/json` · bytes 2058
- list length: 10
- first row keys: `['proxyWallet', 'amount', 'pseudonym', 'name', 'bio', 'profileImage', 'profileImageOptimized']`
- shape:
```json
[
  {
    "proxyWallet": "str(42)",
    "amount": "float",
    "pseudonym": "str:'Theo4'",
    "name": "str:'Theo4'",
    "bio": "str:''",
    "profileImage": "str:''",
    "profileImageOptimized": "str:''"
  }
]
```

## `data:leaderboards_profit`

- GET `https://data-api.polymarket.com/leaderboards/profit?window=week&limit=10`
- status `404` · content-type `text/plain; charset=utf-8` · bytes 19
- snippet: `404 page not found
`

## `data:leaderboards_volume`

- GET `https://data-api.polymarket.com/leaderboards/volume?window=week&limit=10`
- status `404` · content-type `text/plain; charset=utf-8` · bytes 19
- snippet: `404 page not found
`

## `data:positions`

- GET `https://data-api.polymarket.com/positions?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839&limit=5`
- status `200` · content-type `application/json` · bytes 2
- shape:
```json
[]
```

## `data:activity`

- GET `https://data-api.polymarket.com/activity?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839&limit=5`
- status `200` · content-type `application/json` · bytes 4006
- list length: 5
- first row keys: `['proxyWallet', 'timestamp', 'conditionId', 'type', 'size', 'usdcSize', 'transactionHash', 'price', 'asset', 'side', 'outcomeIndex', 'title', 'slug', 'icon', 'eventSlug', 'outcome', 'name', 'pseudonym', 'bio', 'profileImage', 'profileImageOptimized']`
- shape:
```json
[
  {
    "proxyWallet": "str(42)",
    "timestamp": "int",
    "conditionId": "str(66)",
    "type": "str:'REDEEM'",
    "size": "float",
    "usdcSize": "float",
    "transactionHash": "str(66)",
    "price": "int",
    "asset": "str:''",
    "side": "str:''",
    "outcomeIndex": "int",
    "title": "str(47)",
    "slug": "str(65)",
    "icon": "str(135)",
    "eventSlug": "str(65)",
    "outcome": "str:''",
    "name": "str:'Theo4'",
    "pseudonym": "str:'Ironclad-Tenement'",
    "bio": "str:''",
    "profileImage": "str:''"
  }
]
```

## `data:value`

- GET `https://data-api.polymarket.com/value?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839`
- status `200` · content-type `application/json` · bytes 65
- list length: 1
- first row keys: `['user', 'value']`
- shape:
```json
[
  {
    "user": "str(42)",
    "value": "int"
  }
]
```

## `data:trades`

- GET `https://data-api.polymarket.com/trades?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839&limit=5`
- status `200` · content-type `application/json` · bytes 4114
- list length: 5
- first row keys: `['proxyWallet', 'side', 'asset', 'conditionId', 'size', 'price', 'timestamp', 'title', 'slug', 'icon', 'eventSlug', 'outcome', 'outcomeIndex', 'name', 'pseudonym', 'bio', 'profileImage', 'profileImageOptimized', 'transactionHash']`
- shape:
```json
[
  {
    "proxyWallet": "str(42)",
    "side": "str:'BUY'",
    "asset": "str(77)",
    "conditionId": "str(66)",
    "size": "float",
    "price": "float",
    "timestamp": "int",
    "title": "str(42)",
    "slug": "str(41)",
    "icon": "str(135)",
    "eventSlug": "str:'next-james-bond-actor'",
    "outcome": "str:'No'",
    "outcomeIndex": "int",
    "name": "str:'Theo4'",
    "pseudonym": "str:'Ironclad-Tenement'",
    "bio": "str:''",
    "profileImage": "str:''",
    "profileImageOptimized": "str:''",
    "transactionHash": "str(66)"
  }
]
```

## `goldsky:meta`

- POST `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn`
- status `200` · content-type `application/json` · bytes 136
- top-level keys: `['data']`
- shape:
```json
{
  "data": {
    "_meta": {
      "block": "dict",
      "deployment": "str",
      "hasIndexingErrors": "bool"
    }
  }
}
```

## `goldsky:trades_sample`

- POST `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn`
- status `200` · content-type `application/json` · bytes 1359
- top-level keys: `['data']`
- shape:
```json
{
  "data": {
    "orderFilledEvents": [
      "dict"
    ]
  }
}
```
