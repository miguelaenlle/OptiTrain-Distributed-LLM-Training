# Resilience contracts and streaming failover

An intentionally canceled request cannot complete, and a fleet-wide outage
cannot deliver normal service. Test whether these faults are handled correctly
while retaining every lost request in the serving-SLO report.

## Run everything

From the repository root, with the [local build prerequisites](network-testing.md):

```sh
PYTHONPATH=src python -m orchestrator.nettest.suite \
  --config experiments/network/resilience.json --output .fleet/resilience-report
```

Allow about 28 minutes plus build. Read `.fleet/resilience-report/report.md`.
The three-worker, 6 RPS, 512-input/16-output workload, 30/30/60s phases,
H100-derived timing profile and normal SLO are unchanged. No GPU or cloud is used.
The original recipe, results and evidence remain in `experiments/network/report.json`
and `experiments/network/results/`.

Every v2 report contains:

| Result | Meaning |
|---|---|
| Resilience | Scenario-specific failure handling plus healthy/recovery and correctness checks. This determines the command exit status. |
| Normal serving SLO | At least 99% of all offered requests in **each phase** complete with TTFT ≤250ms, **every** output gap ≤100ms and total ≤2s. Failures and cancellations remain in the denominator. |
| Legacy contract | The original acceptance checks evaluated on these new measurements. Includes the original ≥65% fault-SLO allowance and stricter relative gap checks. |

Exit 0 means every resilience test passed; 1 means valid resilience failures;
2 means invalid/incomplete evidence. A green resilience result does **not**
mean the normal serving SLO passed. No fault failure rate or annual availability
is inferred from these stress tests.

## Contracts frozen before traffic

`contract_version: 1` retains the existing evaluator. Opt into version 2 with
the resilience recipe. `contract.json` records the input-derived budget before
the warm-up request. Version 2 currently supports **local CPU replay through
both proxies**; cancellation clock comparisons and wire budgets are specific
to that setup. External real-model/SSH runs retain the original evaluator.

| Scenario | Fault-phase resilience requirement |
|---|---|
| Baseline; client delay; combined delay; worker delay/unreachable/stall/flapping | Original fault checks, including ≥99% completion and TTFT p95 ≤600ms with added p95 ≤350ms. Improve routing to meet them. |
| Client jitter | ≥99% completion and compliance with a fault-only 200ms gap allowance; normal TTFT and total budgets retained. The gap allowance is maximum injected delay (150ms) + profiled gap +25ms scheduling, rounded up to 50ms. |
| Slow download | ≥99% completion and compliance with TTFT ≤550ms, every gap ≤200ms, total ≤1950ms for the shipped profile and 3,000 B/s cap. Formulas below determine these values before measurement. |
| Client disconnect | Each fault-cohort request cancels after five outputs; the worker acknowledges cancellation within 500ms and produces nothing after acknowledgment. No completion is credited. |
| Fleet stall; slow upload | Fault causes real request failures; every failure is explicit and all requests obey the 2s deadline +100ms observation allowance. Completion/normal-SLO loss stays visible. |
| Worker truncation | A cutoff actually closes a connection; a partial stream is reported incomplete; never silently restart committed output. With the breaker enabled, attributable upstream failures must open it. Retain the original ≥65% fault-SLO/goodput and latency checks for service on surviving workers. |
| Idle expiry | Add a 150ms quiet interval after the first replay output during the fault phase. The 50ms idle rule must actually close a connection and the partial stream must be reported incomplete. Retain the original ≥65% fault-SLO/goodput and latency checks for surviving workers. |

Every scenario also requires zero generator drops, at most three upstream
attempts, no retries after committed output, requested output count on completed
streams, termination of every started replay generation, and empty router,
worker and proxy state after draining **before process shutdown**. Healthy and
recovery thresholds are unchanged, including ≥99% completion/compliance,
≥95% restored goodput, and a qualifying five-second recovery window starting
within five seconds of rule removal. Recovery means restored traffic behavior,
not node restart time.

The strengthened idle test is a different fault stimulus from the original
matrix, where regular output never exceeded the idle threshold. Label it as
such when comparing results.

### Bandwidth budget derivation

The controlled replay fixture emits one `x` per SSE event with a bounded
runner-generated request ID and `timing-replay` model name. Budget **256 bytes
per event**, a 13-byte completion marker, and **1,024 bytes** for HTTP headers
and framing. Count actual response-body bytes and reject a fixture exceeding
the declared body bound. These limits do not describe arbitrary model output.

For bandwidth `R`, frame bound `F=256`, pacing slice `Q=max(1,floor(R/20))`:

- First output: profiled first-output time + `1000*(1024+F+Q)/R` +50ms.
- Every output gap: maximum profiled gap + `1000*(F+Q)/R` +50ms.
- Completion: profiled full service time + `1000*(1024+256*N+13)/R` +100ms.

Round each up to 50ms. This allows wire serialization and slice alignment;
it does not choose thresholds from observed percentiles. All original normal-SLO
and legacy checks are still reported.

## Router changes

All new routing behavior is **opt-in and streaming-only**. Buffered requests
retain their existing routing behavior. Primary selection stays round robin.

| Setting | Default | Resilience recipe |
|---|---:|---:|
| `ROUTER_FIRST_OUTPUT_TIMEOUT_SECONDS` | 10s | 0.1s |
| `ROUTER_STREAM_FAILURE_THRESHOLD` | 0 (disabled) | 1 |
| `ROUTER_STREAM_COOLDOWN_SECONDS` | 1s | 1s |
| `ROUTER_STREAM_LEAST_INFLIGHT_RETRIES` | false | true |

The original test recipe uses a 1s first-output timeout; the shipped replay
profile predicts about 12ms. The 100ms setting is a replay experiment, **not**
a recommended timeout for real model serving. Production needs its own measured
first-output distribution.

Reservations atomically track active attempts. Retries choose the least-busy
eligible worker, preserving round robin tie order. Attributable upstream
transport/timeout/protocol/5xx failures open a worker's breaker. After cooldown,
only one request probes that worker; success restores it, failure reopens it.
Client cancellation and downstream write errors do not quarantine workers.
Health is keyed by worker ID **and address**; retired idle entries are removed.
Concurrent successes cannot erase a newer failure. Attempts never revisit a
worker or retry after output commitment.

## Reproduce saved evidence

```sh
PYTHONPATH=src python -m orchestrator.nettest.suite --report-from .fleet/resilience-report
```

This starts no traffic. It checks hashes, rederives budgets from the frozen
profile/config, recomputes request/attempt/event checks, and compares the saved
reports. It does not require the original run directory's executables.
Fresh TCP timings vary with CPU scheduling; only saved-evidence regeneration
is byte-for-byte reproducible. Keep pre-change results and independently repeat
promising routing settings across seeds before treating them as robust gains.
