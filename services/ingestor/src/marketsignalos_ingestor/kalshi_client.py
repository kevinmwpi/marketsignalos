from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from time import sleep, time
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True, slots=True)
class KalshiClientConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    api_key: str | None = None
    api_key_id: str | None = None
    private_key_pem: str | None = None

    @classmethod
    def from_env(cls) -> KalshiClientConfig:
        return cls(
            base_url=os.getenv("KALSHI_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.getenv("KALSHI_API_KEY"),
            api_key_id=os.getenv("KALSHI_API_KEY_ID"),
            private_key_pem=os.getenv("KALSHI_PRIVATE_KEY_PEM"),
        )


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

    def list_fills(
        self,
        ticker: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request_with_retries("GET", "/portfolio/fills", params=params)

    def list_markets(
        self,
        *,
        status: str = "settled",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request_with_retries("GET", "/markets", params=params)

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        retryable_status_codes = {429, 500, 502, 503, 504}

        for attempt in range(self._config.max_retries + 1):
            response = self._client.request(
                method,
                path,
                params=params,
                headers=self._build_keypair_headers(method=method, path=path),
            )
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

    def _build_keypair_headers(self, *, method: str, path: str) -> dict[str, str]:
        if not self._config.api_key_id and not self._config.private_key_pem:
            return {}

        if not self._config.api_key_id or not self._config.private_key_pem:
            raise ValueError(
                "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM must both be set for keypair auth"
            )

        timestamp_ms = str(int(time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._sign_message(message)

        return {
            "KALSHI-ACCESS-KEY": self._config.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _sign_message(self, message: bytes) -> str:
        # Lazy import so non-http tests can run without crypto dependency at import time.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key_pem = self._config.private_key_pem
        if private_key_pem is None:
            raise ValueError("KALSHI_PRIVATE_KEY_PEM must be set for keypair auth")

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi private key must be an RSA private key")

        signed = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signed).decode("ascii")
