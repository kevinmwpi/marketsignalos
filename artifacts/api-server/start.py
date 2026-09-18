"""Portable preview/production entrypoint for the imported Python API."""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    # Resolve the adapter relative to this file, including when launched elsewhere.
    uvicorn.run("api_adapter:app", app_dir=str(Path(__file__).resolve().parent),
                host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
