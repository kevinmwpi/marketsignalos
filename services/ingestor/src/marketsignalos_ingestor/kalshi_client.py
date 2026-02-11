from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class KalshiClientConfig:
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    api_key: str | None = None


class KalshiClient:
    def __init__(self, config: KalshiClientConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        headers: dict[str, str] = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        self._client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def list_trades(
        self,
        ticker: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request_with_retries("GET", "/portfolio/trades", params=params)

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        retryable_status_codes = {429, 500, 502, 503, 504}

        for attempt in range(self._config.max_retries + 1):
            response = self._client.request(method, path, params=params)
            if response.status_code not in retryable_status_codes:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Kalshi API payload must be a JSON object")
                return payload

            if attempt == self._config.max_retries:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(float(retry_after))
            else:
                sleep(self._config.retry_backoff_seconds * (2**attempt))

        raise RuntimeError("Request retry loop exited unexpectedly")
