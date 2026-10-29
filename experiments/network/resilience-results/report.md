# Network failure scenario report

Contract v2: resilience acceptance is separate from the unchanged normal serving SLO and legacy acceptance checks. A resilience PASS never erases failed requests.

Source: `dfee96b5660de567c000723159190b4eeb59c9da`. Environment: Linux-7.0.12-linuxkit-aarch64-with-glibc2.39; Python 3.12.3. Seed: **42**.

Workload: **3 workers, 6.0 RPS, concurrency 32, 512 input / 16 output events**. Phases: 30.0s healthy → 30.0s fault → 60.0s recovery. Scenarios execute sequentially with fresh processes.

Timing source: **model-derived**. Settings: `{"backend": "vllm", "backend_version": "0.24.0", "batch_size": 1, "model": "meta-llama/Meta-Llama-3.1-8B", "pp": 1, "system": "h100_sxm", "tp": 1}`. Model-derived schedules are estimates, not measured GPU serving. Each replay worker runs one request at a time; no continuous batching or KV-cache model.

Normal SLO: complete, TTFT ≤250.0ms, every output gap ≤100.0ms, total ≤2000.0ms. Scenario-specific resilience contracts are shown below; normal-SLO breaches remain visible.

Inputs, source files, binaries and case evidence have SHA-256 records in `suite.json`. Metrics below are recomputed from raw requests/attempts and checked against each original report.

[Recorded evidence bundle](evidence.tar.gz). Extract it to inspect the case files below or regenerate this report without running traffic.

Routing: first-output timeout 0.2s; options `{"cooldown_s": 1.0, "failure_threshold": 2, "least_inflight_retries": true, "probe_when_all_open": true}`.

## Overview

Fault-phase metrics; latency percentiles only describe received output. Failures/drops remain in completion and SLO denominators. N/A means no observed output, not zero latency.

| Scenario | Resilience | Normal SLO | Legacy contract | Completed (all offered) | Fault SLO % | Fault TTFT p95 ms | Fault gap p99 ms | Fault goodput RPS |
|---|---|---|---|---:|---:|---:|---:|---:|
| baseline | PASS | PASS | PASS | 720/720 | 100.00 | 17.08 | 7.61 | 6.00 |
| client-delay | PASS | PASS | PASS | 720/720 | 100.00 | 118.53 | 8.51 | 5.97 |
| client-disconnect | PASS | FAIL | FAIL | 540/720 | 0.00 | 17.38 | 7.83 | 0.00 |
| client-jitter | PASS | FAIL | FAIL | 720/720 | 87.22 | 158.38 | 96.51 | 5.23 |
| combined-path-delay | PASS | PASS | PASS | 720/720 | 100.00 | 220.16 | 9.32 | 6.00 |
| fleet-stall | PASS | FAIL | FAIL | 540/720 | 0.56 | 15.97 | 7.14 | 0.03 |
| idle-expiry | PASS | FAIL | FAIL | 699/720 | 88.33 | 17.10 | 7.53 | 5.30 |
| slow-client-download | PASS | FAIL | FAIL | 720/720 | 22.35 | 67.31 | 101.64 | 1.33 |
| slow-client-upload | PASS | FAIL | FAIL | 549/720 | 0.56 | 1999.24 | 7.58 | 0.03 |
| worker-delay | PASS | PASS | PASS | 720/720 | 100.00 | 214.88 | 7.60 | 5.97 |
| worker-flapping | PASS | PASS | PASS | 720/720 | 100.00 | 214.92 | 7.66 | 6.00 |
| worker-stall | PASS | PASS | PASS | 720/720 | 100.00 | 214.63 | 7.65 | 6.00 |
| worker-truncate | PASS | FAIL | FAIL | 699/720 | 88.33 | 17.03 | 7.61 | 5.30 |
| worker-unreachable | PASS | PASS | PASS | 720/720 | 100.00 | 17.48 | 7.64 | 6.00 |

## baseline

**PASS**. Fault: `No proxy rule.`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "baseline", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.91 | 17.08 (+1.0%) | 16.91 (+0.0%) |
| gap_p99_ms | 7.65 | 7.61 (-0.6%) | 7.62 (-0.4%) |
| goodput_rps | 6.00 | 6.00 (-0.0%) | 6.00 (-0.0%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **0**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `01-baseline/report.txt`, `01-baseline/config.json`, `01-baseline/requests.jsonl`.

## client-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 100, "direction": "response", "seed": 42, "target": "client"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "client-delay", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.09 | 118.53 (+593.7%) | 16.90 (-1.1%) |
| gap_p99_ms | 7.62 | 8.51 (+11.6%) | 7.60 (-0.3%) |
| goodput_rps | 6.02 | 5.97 (-0.9%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `02-client-delay/report.txt`, `02-client-delay/config.json`, `02-client-delay/requests.jsonl`.

## client-disconnect

**PASS**. Fault: `Client cancels after five nonempty output events, for fault-phase dispatches only.`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "client-disconnect", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 0.00 (-100.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.00 (-100.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.07 | 17.38 (+1.8%) | 17.01 (-0.4%) |
| gap_p99_ms | 7.63 | 7.83 (+2.6%) | 7.62 (-0.1%) |
| goodput_rps | 6.00 | 0.00 (-100.0%) | 6.00 (-0.0%) |

All offered: **720**; completed: **540**; boundary/guard requests retained: **0**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `03-client-disconnect/report.txt`, `03-client-disconnect/config.json`, `03-client-disconnect/requests.jsonl`.

## client-jitter

**PASS**. Fault: `{"action": "delay", "delay_ms": 75, "direction": "response", "jitter_ms": 75, "seed": 42, "target": "client"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Maximum injected delay + profiled gap + 25ms scheduling allowance, rounded up to 50ms.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 200, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "client-jitter", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 87.22 (-12.78 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.98 | 158.38 (+832.6%) | 16.89 (-0.6%) |
| gap_p99_ms | 7.66 | 96.51 (+1160.3%) | 7.65 (-0.1%) |
| goodput_rps | 6.02 | 5.23 (-13.1%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `04-client-jitter/report.txt`, `04-client-jitter/config.json`, `04-client-jitter/requests.jsonl`.

## combined-path-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 100, "direction": "response", "seed": 42, "target": "*"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "combined-path-delay", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.98 | 220.16 (+1196.9%) | 16.96 (-0.1%) |
| gap_p99_ms | 7.70 | 9.32 (+21.0%) | 7.75 (+0.6%) |
| goodput_rps | 6.02 | 6.00 (-0.3%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `05-combined-path-delay/report.txt`, `05-combined-path-delay/config.json`, `05-combined-path-delay/requests.jsonl`.

## fleet-stall

**PASS**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "*"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "fleet-stall", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 0.56 (-99.44 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.56 (-99.44 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.87 | 15.97 (-5.3%) | 16.94 (+0.4%) |
| gap_p99_ms | 7.71 | 7.14 (-7.4%) | 7.66 (-0.7%) |
| goodput_rps | 6.02 | 0.03 (-99.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **540**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `06-fleet-stall/report.txt`, `06-fleet-stall/config.json`, `06-fleet-stall/requests.jsonl`.

## idle-expiry

**PASS**. Fault: `{"action": "idle", "direction": "response", "idle_ms": 50, "seed": 42, "target": "worker-1"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 150, "scenario": "idle-expiry", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 88.33 (-11.67 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 88.33 (-11.67 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.20 | 17.10 (-0.6%) | 17.03 (-1.0%) |
| gap_p99_ms | 7.55 | 7.53 (-0.3%) | 7.61 (+0.7%) |
| goodput_rps | 6.02 | 5.30 (-12.0%) | 5.98 (-0.6%) |

All offered: **720**; completed: **699**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `07-idle-expiry/report.txt`, `07-idle-expiry/config.json`, `07-idle-expiry/requests.jsonl`.

## slow-client-download

**PASS**. Fault: `{"action": "bandwidth", "direction": "response", "rate_bytes_s": 3000, "seed": 42, "target": "client"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Bounded SSE frame/HTTP bytes divided by configured bandwidth, replay timing, one pacing quantum and declared 50/100ms scheduling allowances.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 200, "total_ms": 1950, "ttft_ms": 550}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "slow-client-download", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 22.35 (-77.65 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.90 | 67.31 (+298.2%) | 17.11 (+1.2%) |
| gap_p99_ms | 7.64 | 101.64 (+1229.5%) | 7.68 (+0.5%) |
| goodput_rps | 6.02 | 1.33 (-77.9%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `08-slow-client-download/report.txt`, `08-slow-client-download/config.json`, `08-slow-client-download/requests.jsonl`.

## slow-client-upload

**PASS**. Fault: `{"action": "bandwidth", "direction": "request", "rate_bytes_s": 100, "seed": 42, "target": "client"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "slow-client-upload", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 5.03 (-94.97 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.56 (-99.44 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.03 | 1999.24 (+11637.5%) | 17.25 (+1.3%) |
| gap_p99_ms | 7.60 | 7.58 (-0.3%) | 7.69 (+1.2%) |
| goodput_rps | 6.02 | 0.03 (-99.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **549**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.50s after removal; confirmed +5.50s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `09-slow-client-upload/report.txt`, `09-slow-client-upload/config.json`, `09-slow-client-upload/requests.jsonl`.

## worker-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 300, "direction": "response", "seed": 42, "target": "worker-1"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "worker-delay", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.07 | 214.88 (+1158.5%) | 17.09 (+0.1%) |
| gap_p99_ms | 7.67 | 7.60 (-0.9%) | 7.62 (-0.6%) |
| goodput_rps | 6.02 | 5.97 (-0.9%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `10-worker-delay/report.txt`, `10-worker-delay/config.json`, `10-worker-delay/requests.jsonl`.

## worker-flapping

**PASS**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "worker-1"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "worker-flapping", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.27 | 214.92 (+1144.1%) | 17.13 (-0.9%) |
| gap_p99_ms | 7.60 | 7.66 (+0.8%) | 7.63 (+0.4%) |
| goodput_rps | 6.02 | 6.00 (-0.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `11-worker-flapping/report.txt`, `11-worker-flapping/config.json`, `11-worker-flapping/requests.jsonl`.

## worker-stall

**PASS**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "worker-1"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "worker-stall", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.10 | 214.63 (+1155.1%) | 17.00 (-0.6%) |
| gap_p99_ms | 7.64 | 7.65 (+0.2%) | 7.64 (+0.0%) |
| goodput_rps | 6.02 | 6.00 (-0.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `12-worker-stall/report.txt`, `12-worker-stall/config.json`, `12-worker-stall/requests.jsonl`.

## worker-truncate

**PASS**. Fault: `{"action": "cutoff", "bytes": 1024, "direction": "response", "seed": 42, "target": "worker-1"}`

Normal serving SLO: **FAIL**; legacy acceptance: **FAIL**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "worker-truncate", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 88.33 (-11.67 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 88.33 (-11.67 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 16.98 | 17.03 (+0.3%) | 17.05 (+0.4%) |
| gap_p99_ms | 7.60 | 7.61 (+0.2%) | 7.61 (+0.1%) |
| goodput_rps | 6.02 | 5.30 (-12.0%) | 5.98 (-0.6%) |

All offered: **720**; completed: **699**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (29 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `13-worker-truncate/report.txt`, `13-worker-truncate/config.json`, `13-worker-truncate/requests.jsonl`.

## worker-unreachable

**PASS**. Fault: `{"action": "disable", "direction": "request", "seed": 42, "target": "worker-1"}`

Normal serving SLO: **PASS**; legacy acceptance: **PASS**.

Frozen fault contract: `{"body_bytes_bound": 4110, "budget_basis": "Normal contract; attributable upstream failures may fail over.", "cancellation_ms": 500, "deadline_slack_ms": 100, "fault_slo": {"gap_ms": 100.0, "total_ms": 2000.0, "ttft_ms": 250.0}, "frame_bytes_bound": 256, "http_overhead_bytes_bound": 1024, "quiet_ms": 0, "scenario": "worker-unreachable", "version": 2}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.23 | 17.48 (+1.4%) | 17.14 (-0.5%) |
| gap_p99_ms | 7.59 | 7.64 (+0.7%) | 7.65 (+0.8%) |
| goodput_rps | 6.02 | 6.00 (-0.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `14-worker-unreachable/report.txt`, `14-worker-unreachable/config.json`, `14-worker-unreachable/requests.jsonl`.

## Reproduction boundaries

The recipe, order, seed and report calculation are fixed. Regenerating from unchanged saved evidence with this source produces identical Markdown/JSON. Fresh TCP runs will have different timing samples because of CPU scheduling, buffering and TCP chunking; a seed does not make live networking deterministic. Compare distributions and verdicts, not bitwise-equal live timings. Each scenario here is one run, not a confidence interval.

The pre-fault deadline guard and control-boundary exclusions are explicit; those requests are still in all-offered totals. Inter-output events are not necessarily individual tokens. No natural failure likelihood, EC2 performance or GPU throughput is inferred.
