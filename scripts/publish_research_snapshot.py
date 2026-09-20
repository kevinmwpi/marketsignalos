"""Publish a complete capture to a dedicated data branch using an ephemeral Actions token."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from collect_research_snapshot import MAX_SNAPSHOT_BYTES

REPOSITORY = "kevinmwpi/marketsignalos"
BRANCH = "codex/research-snapshots"
FILE = "research-snapshot.json"
LOG = logging.getLogger(__name__)


def publication_bytes(path: Path, source_commit: str, run_id: str) -> bytes:
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("Candidate exceeds publication size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Automated publication has a stricter gate than manual descriptive captures.
    # A degraded refresh must not replace the last complete public capture.
    if (
        data.get("schema_version") != 1
        or data.get("status") != "complete"
        or data.get("source") != "https://data-api.polymarket.com"
    ):
        raise ValueError(
            "Only complete, supported captures may be published automatically"
        )
    wallets = data["wallets"]
    if not wallets or len(wallets) != data["limits"]["wallets"] or len(wallets) > 20:
        raise ValueError("Wallet coverage differs from the requested cohort size")
    if len({w["address"] for w in wallets}) != len(wallets):
        raise ValueError("Duplicate wallets")
    if len(data["requests"]) != 1 + 2 * len(wallets) or any(
        r["status"] != "ok" or r.get("rows_rejected") for r in data["requests"]
    ):
        raise ValueError("Incomplete request coverage")
    if any(
        w["evaluation_status"] != "not_evaluated"
        or any(
            w[k]["status"] != "ok" or w[k]["rows_rejected"]
            for k in ("trades", "positions")
        )
        for w in wallets
    ):
        raise ValueError("Unusable wallet coverage")
    age = (
        datetime.now(UTC) - datetime.fromisoformat(data["generated_at"])
    ).total_seconds()
    if not 0 <= age <= 3600:
        raise ValueError("Capture is not from the current collection run")
    data["publication"] = {
        "source_commit": source_commit,
        "workflow_run": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "schedule_hours": 6,
    }
    content = (
        json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Published capture exceeds size limit")
    return content


def publish(client: httpx.Client, content: bytes) -> str:
    prefix = f"/repos/{REPOSITORY}/git"

    def call(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = client.request(method, prefix + path, json=body)
        response.raise_for_status()
        return response.json()

    response = client.get(prefix + f"/ref/heads/{BRANCH}")
    if response.status_code == 404:
        parent = None
    else:
        response.raise_for_status()
        parent = response.json()["object"]["sha"]
    # Construct an isolated tree; never check out, edit, or push application branches.
    blob = call(
        "POST",
        "/blobs",
        {"content": base64.b64encode(content).decode(), "encoding": "base64"},
    )
    expected_blob = hashlib.sha1(
        b"blob " + str(len(content)).encode() + b"\0" + content
    ).hexdigest()
    if blob["sha"] != expected_blob:
        raise ValueError("Published blob digest mismatch")
    tree = call(
        "POST",
        "/trees",
        {
            "tree": [
                {"path": FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            ]
        },
    )
    commit = call(
        "POST",
        "/commits",
        {
            "message": "Refresh bounded public research snapshot [skip ci]",
            "tree": tree["sha"],
            "parents": [parent] if parent else [],
        },
    )
    if parent:
        call("PATCH", f"/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})
    else:
        call("POST", "/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    actual = call("GET", f"/ref/heads/{BRANCH}")
    if actual["object"]["sha"] != commit["sha"]:
        raise ValueError("Data branch read-back differs from published commit")
    return commit["sha"]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise RuntimeError(
            "Publication is restricted to this repository's main-branch workflow"
        )
    content = publication_bytes(
        Path(sys.argv[1]), os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"]
    )
    with httpx.Client(
        base_url="https://api.github.com",
        timeout=20,
        follow_redirects=False,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as client:
        commit = publish(client, content)
    digest = hashlib.sha256(content).hexdigest()
    LOG.info("Published data commit %s; sha256=%s", commit, digest)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(
                f"Published complete research capture to `{BRANCH}`.\n\nCommit: `{commit}`\n\nSHA-256: `{digest}`\n"
            )


if __name__ == "__main__":
    main()
