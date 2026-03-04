This is a [Next.js](https://nextjs.org) dashboard for MarketSignalOS.

## Getting Started

First, run the development server:

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser.

## API dependency

The homepage fetches latest trades from:
- `${NEXT_PUBLIC_API_BASE_URL}/signals/trades?limit=20`
- `${NEXT_PUBLIC_API_BASE_URL}/signals/suspicious-accounts?fresh_days=30&min_resolved=20&limit=20`

Set `NEXT_PUBLIC_API_BASE_URL` in your environment if your API is not running on `http://localhost:8000`.
