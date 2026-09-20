import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_research_snapshot as publisher


@pytest.fixture
def candidate(tmp_path):
    root = Path(__file__).resolve().parents[2]
    data = json.loads(
        (
            root
            / "artifacts/marketsignalos-dashboard/public/data/research-snapshot.json"
        ).read_text(encoding="utf-8")
    )
    data["generated_at"] = datetime.now(UTC).isoformat()
    output = tmp_path / "candidate.json"
    output.write_text(json.dumps(data), encoding="utf-8")
    return output


def test_publication_contains_traceable_run_metadata(candidate):
    content = publisher.publication_bytes(candidate, "a" * 40, "123")
    data = json.loads(content)
    assert data["publication"] == {
        "source_commit": "a" * 40,
        "workflow_run": "https://github.com/kevinmwpi/marketsignalos/actions/runs/123",
        "schedule_hours": 6,
    }


@pytest.mark.parametrize(
    "damage", ["partial", "coverage", "duplicates", "request_failure", "old", "future"]
)
def test_degraded_candidate_cannot_be_published(candidate, damage):
    data = json.loads(candidate.read_text())
    if damage == "partial":
        data["status"] = "partial"
    elif damage == "coverage":
        data["wallets"].pop()
    elif damage == "duplicates":
        data["wallets"][-1] = data["wallets"][0]
    elif damage == "request_failure":
        data["requests"][-1]["status"] = "unavailable"
    else:
        data["generated_at"] = (
            datetime.now(UTC) + timedelta(hours=2 if damage == "future" else -2)
        ).isoformat()
    candidate.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        publisher.publication_bytes(candidate, "a" * 40, "123")


@pytest.mark.parametrize("existing", [True, False])
def test_data_branch_only_with_atomic_nonforce_readback(existing):
    content = b'{"example":1}\n'
    blob_sha = hashlib.sha1(
        b"blob " + str(len(content)).encode() + b"\0" + content
    ).hexdigest()
    calls = []
    ref_reads = 0

    def handler(request):
        nonlocal ref_reads
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET":
            assert path.endswith("/ref/heads/codex/research-snapshots")
            ref_reads += 1
            if ref_reads == 1 and not existing:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"object": {"sha": "parent" if ref_reads == 1 else "new"}}
            )
        body = json.loads(request.content)
        if path.endswith("/blobs"):
            assert body["encoding"] == "base64"
            return httpx.Response(201, json={"sha": blob_sha})
        if path.endswith("/trees"):
            assert body["tree"] == [
                {
                    "path": "research-snapshot.json",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ]
            return httpx.Response(201, json={"sha": "tree"})
        if path.endswith("/commits"):
            assert body["parents"] == (["parent"] if existing else [])
            return httpx.Response(201, json={"sha": "new"})
        if existing:
            assert path.endswith("/refs/heads/codex/research-snapshots")
            assert body == {"sha": "new", "force": False}
        else:
            assert body == {"ref": "refs/heads/codex/research-snapshots", "sha": "new"}
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        assert publisher.publish(client, content) == "new"
    assert len(calls) == 6
    assert ref_reads == 2


def test_digest_failure_never_moves_a_branch():
    calls = []

    def handler(request):
        calls.append(request.method)
        return (
            httpx.Response(404)
            if request.method == "GET"
            else httpx.Response(201, json={"sha": "wrong"})
        )

    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client, pytest.raises(ValueError, match="digest mismatch"):
        publisher.publish(client, b"example")
    assert calls == ["GET", "POST"]


def test_publication_requires_the_main_branch_workflow(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", publisher.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/not-main")
    with pytest.raises(RuntimeError, match="main-branch"):
        publisher.main()
