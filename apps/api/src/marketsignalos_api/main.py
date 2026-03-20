from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from marketsignalos_api.api.routes.health import router as health_router
from marketsignalos_api.api.routes.leaderboard import router as leaderboard_router
from marketsignalos_api.api.routes.orderflow import router as orderflow_router
from marketsignalos_api.api.routes.trades import router as trades_router


LANDING_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>MarketSignalOS</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #09090b;
        --panel: rgba(24, 24, 27, 0.92);
        --panel-alt: rgba(39, 39, 42, 0.9);
        --border: rgba(161, 161, 170, 0.25);
        --text: #f4f4f5;
        --muted: #a1a1aa;
        --accent: #34d399;
        --warning-bg: rgba(120, 53, 15, 0.45);
        --warning-border: rgba(251, 191, 36, 0.3);
        --warning-text: #fde68a;
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at top, rgba(52, 211, 153, 0.18), transparent 30%),
          linear-gradient(180deg, #111827 0%, var(--bg) 45%, #030712 100%);
        color: var(--text);
      }

      main {
        max-width: 1120px;
        margin: 0 auto;
        padding: 48px 24px 72px;
      }

      .hero {
        display: grid;
        gap: 24px;
        margin-bottom: 24px;
      }

      .hero-card,
      .table-card {
        border: 1px solid var(--border);
        border-radius: 20px;
        background: var(--panel);
        backdrop-filter: blur(12px);
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
      }

      .hero-card {
        padding: 28px;
      }

      h1 {
        margin: 0 0 12px;
        font-size: clamp(2.2rem, 4vw, 3.5rem);
        line-height: 1.05;
      }

      .lede {
        max-width: 760px;
        margin: 0;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.7;
      }

      .actions {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 24px;
      }

      .button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 10px 16px;
        border-radius: 999px;
        border: 1px solid transparent;
        text-decoration: none;
        font-weight: 600;
        transition: 160ms ease;
      }

      .button-primary {
        background: var(--accent);
        color: #052e16;
      }

      .button-secondary {
        border-color: var(--border);
        color: var(--text);
      }

      .button:hover {
        transform: translateY(-1px);
        opacity: 0.95;
      }

      .stats {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin-top: 28px;
      }

      .stat {
        padding: 18px;
        border-radius: 16px;
        border: 1px solid var(--border);
        background: var(--panel-alt);
      }

      .stat-label {
        display: block;
        font-size: 0.84rem;
        color: var(--muted);
      }

      .stat-value {
        display: block;
        margin-top: 6px;
        font-size: 1.8rem;
        font-weight: 700;
      }

      .table-card {
        overflow: hidden;
      }

      .table-header {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        padding: 20px 24px 12px;
      }

      .table-header h2 {
        margin: 0;
        font-size: 1.2rem;
      }

      .table-header p {
        margin: 6px 0 0;
        color: var(--muted);
      }

      .table-wrap {
        overflow-x: auto;
      }

      table {
        width: 100%;
        border-collapse: collapse;
        min-width: 880px;
      }

      th,
      td {
        padding: 14px 16px;
        text-align: left;
        border-top: 1px solid rgba(161, 161, 170, 0.12);
      }

      th {
        font-size: 0.76rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        background: rgba(24, 24, 27, 0.92);
      }

      tbody tr:hover {
        background: rgba(63, 63, 70, 0.24);
      }

      .mono {
        font-family: ui-monospace, SFMono-Regular, SFMono-Regular, Menlo, monospace;
      }

      .warning {
        margin: 0 24px 20px;
        padding: 14px 16px;
        border-radius: 14px;
        border: 1px solid var(--warning-border);
        background: var(--warning-bg);
        color: var(--warning-text);
      }

      .empty {
        padding: 24px;
        color: var(--muted);
      }

      footer {
        margin-top: 18px;
        color: var(--muted);
        font-size: 0.92rem;
      }

      @media (max-width: 720px) {
        main {
          padding: 28px 16px 48px;
        }

        .hero-card {
          padding: 22px;
        }

        .table-header {
          padding: 18px 18px 10px;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="hero-card">
          <h1>MarketSignalOS skill leaderboard</h1>
          <p class="lede">
            Production deployment is now serving a lightweight frontend directly from the API service,
            so the Railway root URL shows ranked accounts instead of redirecting to Swagger docs.
          </p>
          <div class="actions">
            <a class="button button-primary" href="#leaderboard">View leaderboard</a>
            <a class="button button-secondary" href="/docs">API docs</a>
            <a class="button button-secondary" href="/health">Health check</a>
          </div>
          <div class="stats">
            <div class="stat">
              <span class="stat-label">Leaderboard status</span>
              <span class="stat-value" id="status-value">Loading…</span>
            </div>
            <div class="stat">
              <span class="stat-label">Accounts shown</span>
              <span class="stat-value" id="accounts-value">--</span>
            </div>
            <div class="stat">
              <span class="stat-label">Top skill score</span>
              <span class="stat-value" id="skill-value">--</span>
            </div>
          </div>
        </div>
      </section>

      <section class="table-card" id="leaderboard">
        <div class="table-header">
          <div>
            <h2>Top accounts by modeled skill</h2>
            <p>Data source: <span class="mono">/signals/leaderboard?fresh_days=30&min_resolved=20&limit=25</span></p>
          </div>
        </div>
        <div id="error-banner" class="warning" hidden></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Account</th>
                <th>Insider-like</th>
                <th>Skill</th>
                <th>Anomaly</th>
                <th>Z-score</th>
                <th>Resolved</th>
                <th>Win rate</th>
                <th>Last activity</th>
              </tr>
            </thead>
            <tbody id="leaderboard-body">
              <tr>
                <td class="empty" colspan="9">Loading leaderboard…</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <footer>
        Need raw endpoints too? The REST surface remains available under <span class="mono">/signals/*</span>,
        with interactive docs still at <span class="mono">/docs</span>.
      </footer>
    </main>

    <script>
      const endpoint = "/signals/leaderboard?fresh_days=30&min_resolved=20&limit=25";
      const body = document.getElementById("leaderboard-body");
      const errorBanner = document.getElementById("error-banner");
      const statusValue = document.getElementById("status-value");
      const accountsValue = document.getElementById("accounts-value");
      const skillValue = document.getElementById("skill-value");

      const formatPercent = (value) => `${(value * 100).toFixed(1)}%`;

      const showError = (message) => {
        errorBanner.hidden = false;
        errorBanner.textContent = message;
        statusValue.textContent = "Attention needed";
      };

      fetch(endpoint, { cache: "no-store" })
        .then(async (response) => {
          if (!response.ok) {
            throw new Error(`Leaderboard API returned ${response.status}`);
          }
          return response.json();
        })
        .then((rows) => {
          statusValue.textContent = "Live";
          accountsValue.textContent = String(rows.length);
          skillValue.textContent = rows.length ? formatPercent(rows[0].skill_likelihood) : "--";

          if (!rows.length) {
            body.innerHTML = '<tr><td class="empty" colspan="9">No qualifying accounts yet.</td></tr>';
            return;
          }

          body.innerHTML = rows.map((row, index) => `
            <tr>
              <td>${index + 1}</td>
              <td class="mono">${row.account_id}</td>
              <td>${formatPercent(row.insider_like_score)}</td>
              <td>${formatPercent(row.skill_likelihood)}</td>
              <td>${formatPercent(row.anomaly_probability)}</td>
              <td>${row.stddevs_above_expected.toFixed(2)}</td>
              <td>${row.resolved_calls}</td>
              <td>${formatPercent(row.win_rate)}</td>
              <td>${row.last_activity_at}</td>
            </tr>
          `).join("");
        })
        .catch((error) => {
          showError(error.message || "Unable to reach leaderboard API.");
          body.innerHTML = '<tr><td class="empty" colspan="9">Unable to load leaderboard data.</td></tr>';
          accountsValue.textContent = "0";
          skillValue.textContent = "--";
        });
    </script>
  </body>
</html>
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="MarketSignalOS API",
        version="0.1.0",
    )

    app.include_router(health_router)
    app.include_router(trades_router)
    app.include_router(leaderboard_router)
    app.include_router(orderflow_router)


    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        return HTMLResponse(content=LANDING_PAGE_HTML)

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    def metrics() -> PlainTextResponse:
        # Prometheus text exposition format
        data = generate_latest()
        return PlainTextResponse(
            content=data.decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()
