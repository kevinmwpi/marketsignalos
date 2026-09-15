---
name: API artifact path mounting
description: Imported services must expose the artifact's full proxy path themselves.
---

Artifact service paths are not stripped by the shared proxy. When preserving an imported API behind an `/api` artifact path, mount the original application under `/api` and keep the artifact health check under that same prefix.

**Why:** The imported FastAPI app originally served routes at `/`, while Replit forwarded `/api/...` unchanged; a direct workflow looked healthy but every browser API request returned 404.

**How to apply:** Check the actual proxy URL with curl through `localhost:80` after wiring any imported backend, and verify both the health endpoint and one real product endpoint.