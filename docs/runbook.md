# Runbook

## Local dev
Not yet implemented. This will be added after ingestion and API scaffolding are complete.

## Common failure modes to handle
- rate limiting (retry with jitter + backoff)
- partial outages / missing data windows
- schema drift (options chain fields change)
- duplicates / out-of-order events

## Operational checks
- ingestion lag within threshold
- completeness >= expected
- signal freshness SLA met
