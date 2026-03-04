This web app renders the MarketSignalOS account skill leaderboard.

## Getting Started

First, set API base and run the development server:

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```

The app queries:
- `${NEXT_PUBLIC_API_BASE_URL}/signals/leaderboard?fresh_days=30&min_resolved=20&limit=25`

Open [http://localhost:3000](http://localhost:3000) to view the leaderboard.
