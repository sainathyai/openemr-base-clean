# Operations: health, readiness, metrics, load

The Week-1 operational surface for the serving layer (`server/main.py`). Complements
[OBSERVABILITY.md](OBSERVABILITY.md) (the Langfuse model-path signal) and
[TESTING.md](TESTING.md) (unit + golden eval).

## Liveness vs readiness

Two separate probes, because "the process is up" and "the process can serve" are
different failures and an orchestrator must treat them differently.

| Probe | Endpoint | Checks | Fails when |
|-------|----------|--------|-----------|
| Liveness | `GET /api/health` | process answers; which narrator is wired | process is wedged (restart it) |
| Readiness | `GET /api/ready` | data source reachable (roster loads); narrator constructible; Langfuse status (non-gating) | data source (OpenEMR FHIR / fixtures snapshot) is unreachable -> **HTTP 503** |

Readiness returns 503 (not an exception) when the gating dependency is down, so a load
balancer stops routing to this instance while liveness still passes. Langfuse being
off never fails readiness: observability is not on the serving path.

## Metrics + dashboard

`GET /api/metrics` is an in-process snapshot (no external store, fits the t3.micro):

- per-route **request count, error count, p50 / p95 / max latency** (recorded by an
  HTTP middleware, keyed by the route *template* so all patients share one series)
- **error rate** overall
- **verification pass / fail** counts and pass-rate (mirrored from UC-1)
- **tool calls** (UC-3) and **retries** (upstream failures)

`GET /dashboard` renders these live (2s poll). The division of labor:

- **request-side** signal (rate, errors, latency percentiles) is served here in-process
- **model-side** signal (per-tool spans, token cost, verification / grounding scores
  *over time*) lives in Langfuse, which is built for time-series and per-trace drill-down

A single Prometheus/Grafana stack would replace the in-process collector in a real
deployment; it is intentionally omitted so the whole demo stays one container.

## Concurrency load test

A closed-loop concurrency / SLO test (`loadtest/run.py`, pure `httpx`, no external
tool). It holds N simulated clinicians hammering a realistic endpoint mix
(schedule 25%, briefing 35%, chat 20%, order-check 20%) and reports latency
percentiles, error rate, and throughput. This is a **concurrency envelope** test, not
an infra-ceiling test: the goal is to show the path stays stable and error-free at 10
and 50 concurrent users.

Run against a server with Langfuse flushing off, so the numbers are the app's own
latency, not the observability network sink:

```
LANGFUSE_PUBLIC_KEY= LANGFUSE_SECRET_KEY= COPILOT_FIXTURES=app/data/fixtures \
  COPILOT_FORCE_STUB=1 python -m uvicorn server.main:app --port 8099
python -m loadtest.run --url http://127.0.0.1:8099 --levels 10,50 --duration 15
```

### Results (single uvicorn worker, fixtures + stub, 15s per level)

| Users | Throughput | Errors | p50 | p95 | p99 |
|------:|-----------:|-------:|----:|----:|----:|
| 10 | 129 req/s | 0 (0.00%) | 54 ms | 184 ms | 228 ms |
| 50 | 135 req/s | 0 (0.00%) | 253 ms | 812 ms | 870 ms |

Per-endpoint p95 at 50 users: briefing (UC-1) 857 ms, chat (UC-3) 360 ms,
order-check (UC-2) 272 ms, schedule 191 ms. UC-1 is the heaviest as expected
(change detection + verification per request).

**Reading it:** one worker saturates near ~130 req/s, so going from 10 to 50 users
raises latency (requests queue) but **error rate stays 0** and p99 stays under ~900 ms.
The system degrades gracefully rather than failing. Horizontal capacity is a matter of
worker/replica count (each replica adds ~130 req/s); the 3-tier deploy already isolates
the agent tier so it scales without touching DB/EMR.
