# Network failure scenario report

Generated from recorded measurements. FAIL means the unchanged acceptance contract was missed; INVALID means setup/execution/evidence was invalid. Neither is converted to PASS.

Source: `28ff966fc55289ae1128d46081016ebd604bd413`. Environment: Linux-7.0.12-linuxkit-aarch64-with-glibc2.39; Python 3.12.3. Seed: **42**.

Workload: **3 workers, 6.0 RPS, concurrency 32, 512 input / 16 output events**. Phases: 30.0s healthy → 30.0s fault → 60.0s recovery. Scenarios execute sequentially with fresh processes.

Timing source: **model-derived**. Settings: `{"backend": "vllm", "backend_version": "0.24.0", "batch_size": 1, "model": "meta-llama/Meta-Llama-3.1-8B", "pp": 1, "system": "h100_sxm", "tp": 1}`. Model-derived schedules are estimates, not measured GPU serving. Each replay worker runs one request at a time; no continuous batching or KV-cache model.

Normal SLO: complete, TTFT ≤250.0ms, every output gap ≤100.0ms, total ≤2000.0ms. The same acceptance criteria apply to every scenario; severe intentional outages can fail them.

Inputs, source files, binaries and case evidence have SHA-256 records in `suite.json`. Metrics below are recomputed from raw requests/attempts and checked against each original report.

[Recorded evidence bundle](evidence.tar.gz). Extract it to inspect the case files below or regenerate this report without running traffic.

## Overview

Fault-phase metrics; latency percentiles only describe received output. Failures/drops remain in completion and SLO denominators. N/A means no observed output, not zero latency.

| Scenario | Verdict | Completed (all offered) | Fault SLO % | Fault TTFT p95 ms | Fault gap p99 ms | Fault goodput RPS |
|---|---|---:|---:|---:|---:|---:|
| baseline | PASS | 720/720 | 100.00 | 17.51 | 7.53 | 6.03 |
| client-delay | PASS | 720/720 | 100.00 | 118.81 | 8.53 | 5.97 |
| client-disconnect | FAIL | 540/720 | 0.00 | 18.19 | 7.64 | 0.00 |
| client-jitter | FAIL | 720/720 | 88.83 | 162.42 | 92.28 | 5.30 |
| combined-path-delay | PASS | 720/720 | 100.00 | 218.48 | 9.01 | 6.00 |
| fleet-stall | FAIL | 551/720 | 0.56 | 1843.39 | 19.74 | 0.03 |
| idle-expiry | PASS | 720/720 | 100.00 | 17.81 | 7.68 | 6.00 |
| slow-client-download | FAIL | 720/720 | 32.96 | 67.83 | 101.37 | 1.97 |
| slow-client-upload | FAIL | 551/720 | 0.56 | 1956.98 | 7.34 | 0.03 |
| worker-delay | PASS | 720/720 | 66.48 | 320.56 | 7.96 | 3.97 |
| worker-flapping | FAIL | 720/720 | 86.67 | 1015.43 | 7.71 | 5.20 |
| worker-stall | FAIL | 720/720 | 67.22 | 1016.79 | 7.55 | 4.03 |
| worker-truncate | FAIL | 660/720 | 66.48 | 17.55 | 7.59 | 3.97 |
| worker-unreachable | PASS | 720/720 | 100.00 | 17.57 | 7.68 | 6.00 |

## baseline

**PASS**. Fault: `No proxy rule.`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.61 | 17.51 (-0.5%) | 17.57 (-0.2%) |
| gap_p99_ms | 7.54 | 7.53 (-0.1%) | 7.58 (+0.5%) |
| goodput_rps | 6.00 | 6.03 (+0.6%) | 5.98 (-0.3%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **0**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `01-baseline/report.txt`, `01-baseline/config.json`, `01-baseline/requests.jsonl`.

## client-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 100, "direction": "response", "seed": 42, "target": "client"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.89 | 118.81 (+564.1%) | 17.79 (-0.5%) |
| gap_p99_ms | 7.55 | 8.53 (+12.9%) | 7.62 (+0.9%) |
| goodput_rps | 6.02 | 5.97 (-0.9%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `02-client-delay/report.txt`, `02-client-delay/config.json`, `02-client-delay/requests.jsonl`.

## client-disconnect

**FAIL**. Fault: `Client cancels after five nonempty output events, for fault-phase dispatches only.`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 0.00 (-100.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.00 (-100.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.65 | 18.19 (+3.1%) | 17.64 (-0.1%) |
| gap_p99_ms | 7.65 | 7.64 (-0.2%) | 7.74 (+1.2%) |
| goodput_rps | 6.00 | 0.00 (-100.0%) | 6.00 (+0.0%) |

All offered: **720**; completed: **540**; boundary/guard requests retained: **0**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (31 requests). This is not node restart time.

Failed checks:

- fault: completion %: 0.00; required >= 99.00.
- fault: normal-SLO compliance %: 0.00; required >= 65.00.
- fault: compliance drop (pp): 100.00; required <= 35.00.
- fault: goodput: 0.00; required >= 3.90.

Evidence in bundle: `03-client-disconnect/report.txt`, `03-client-disconnect/config.json`, `03-client-disconnect/requests.jsonl`.

## client-jitter

**FAIL**. Fault: `{"action": "delay", "delay_ms": 75, "direction": "response", "jitter_ms": 75, "seed": 42, "target": "client"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 88.83 (-11.17 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.56 | 162.42 (+824.7%) | 17.86 (+1.7%) |
| gap_p99_ms | 7.68 | 92.28 (+1101.4%) | 7.65 (-0.4%) |
| goodput_rps | 6.02 | 5.30 (-12.0%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: gap p99 ms: 92.28; required <= 50.00.
- fault: gap versus healthy: 92.28; required <= 9.60.

Evidence in bundle: `04-client-jitter/report.txt`, `04-client-jitter/config.json`, `04-client-jitter/requests.jsonl`.

## combined-path-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 100, "direction": "response", "seed": 42, "target": "*"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 18.02 | 218.48 (+1112.5%) | 17.97 (-0.3%) |
| gap_p99_ms | 7.61 | 9.01 (+18.4%) | 7.62 (+0.1%) |
| goodput_rps | 6.02 | 6.00 (-0.3%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `05-combined-path-delay/report.txt`, `05-combined-path-delay/config.json`, `05-combined-path-delay/requests.jsonl`.

## fleet-stall

**FAIL**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "*"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 6.15 (-93.85 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.56 (-99.44 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.64 | 1843.39 (+10347.1%) | 17.56 (-0.5%) |
| gap_p99_ms | 7.59 | 19.74 (+159.9%) | 7.61 (+0.2%) |
| goodput_rps | 6.02 | 0.03 (-99.4%) | 5.99 (-0.6%) |

All offered: **720**; completed: **551**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: completion %: 6.15; required >= 99.00.
- fault: normal-SLO compliance %: 0.56; required >= 65.00.
- fault: compliance drop (pp): 99.44; required <= 35.00.
- fault: goodput: 0.03; required >= 3.91.
- fault: TTFT p95 ms: 1843.39; required <= 600.00.
- fault: added TTFT p95 ms: 1825.74; required <= 350.00.
- fault: gap versus healthy: 19.74; required <= 9.49.

Evidence in bundle: `06-fleet-stall/report.txt`, `06-fleet-stall/config.json`, `06-fleet-stall/requests.jsonl`.

## idle-expiry

**PASS**. Fault: `{"action": "idle", "direction": "response", "idle_ms": 50, "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.50 | 17.81 (+1.8%) | 17.92 (+2.4%) |
| gap_p99_ms | 7.70 | 7.68 (-0.3%) | 7.70 (+0.0%) |
| goodput_rps | 6.02 | 6.00 (-0.4%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (29 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `07-idle-expiry/report.txt`, `07-idle-expiry/config.json`, `07-idle-expiry/requests.jsonl`.

## slow-client-download

**FAIL**. Fault: `{"action": "bandwidth", "direction": "response", "rate_bytes_s": 3000, "seed": 42, "target": "client"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 32.96 (-67.04 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.86 | 67.83 (+279.9%) | 17.76 (-0.5%) |
| gap_p99_ms | 7.66 | 101.37 (+1223.9%) | 7.58 (-1.1%) |
| goodput_rps | 6.02 | 1.97 (-67.3%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: normal-SLO compliance %: 32.96; required >= 65.00.
- fault: compliance drop (pp): 67.04; required <= 35.00.
- fault: goodput: 1.97; required >= 3.91.
- fault: gap p99 ms: 101.37; required <= 50.00.
- fault: gap versus healthy: 101.37; required <= 9.57.

Evidence in bundle: `08-slow-client-download/report.txt`, `08-slow-client-download/config.json`, `08-slow-client-download/requests.jsonl`.

## slow-client-upload

**FAIL**. Fault: `{"action": "bandwidth", "direction": "request", "rate_bytes_s": 100, "seed": 42, "target": "client"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 5.59 (-94.41 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 0.56 (-99.44 pp) | 99.72 (-0.28 pp) |
| ttft_p95_ms | 17.71 | 1956.98 (+10948.9%) | 17.31 (-2.2%) |
| gap_p99_ms | 7.62 | 7.34 (-3.6%) | 7.57 (-0.7%) |
| goodput_rps | 6.02 | 0.03 (-99.4%) | 5.97 (-0.9%) |

All offered: **720**; completed: **551**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.25s after removal; confirmed +5.25s (30 requests). This is not node restart time.

Failed checks:

- fault: completion %: 5.59; required >= 99.00.
- fault: normal-SLO compliance %: 0.56; required >= 65.00.
- fault: compliance drop (pp): 99.44; required <= 35.00.
- fault: goodput: 0.03; required >= 3.91.
- fault: TTFT p95 ms: 1956.98; required <= 600.00.
- fault: added TTFT p95 ms: 1939.27; required <= 350.00.

Evidence in bundle: `09-slow-client-upload/report.txt`, `09-slow-client-upload/config.json`, `09-slow-client-upload/requests.jsonl`.

## worker-delay

**PASS**. Fault: `{"action": "delay", "delay_ms": 300, "direction": "response", "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 66.48 (-33.52 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.77 | 320.56 (+1704.1%) | 17.55 (-1.2%) |
| gap_p99_ms | 7.65 | 7.96 (+3.9%) | 7.69 (+0.4%) |
| goodput_rps | 6.02 | 3.97 (-34.1%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `10-worker-delay/report.txt`, `10-worker-delay/config.json`, `10-worker-delay/requests.jsonl`.

## worker-flapping

**FAIL**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 86.67 (-13.33 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.92 | 1015.43 (+5566.8%) | 17.60 (-1.8%) |
| gap_p99_ms | 7.74 | 7.71 (-0.3%) | 7.76 (+0.3%) |
| goodput_rps | 6.02 | 5.20 (-13.6%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: TTFT p95 ms: 1015.43; required <= 600.00.
- fault: added TTFT p95 ms: 997.51; required <= 350.00.

Evidence in bundle: `11-worker-flapping/report.txt`, `11-worker-flapping/config.json`, `11-worker-flapping/requests.jsonl`.

## worker-stall

**FAIL**. Fault: `{"action": "stall", "direction": "response", "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 67.22 (-32.78 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.90 | 1016.79 (+5581.7%) | 17.83 (-0.3%) |
| gap_p99_ms | 7.57 | 7.55 (-0.3%) | 7.58 (+0.2%) |
| goodput_rps | 6.02 | 4.03 (-33.0%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: TTFT p95 ms: 1016.79; required <= 600.00.
- fault: added TTFT p95 ms: 998.90; required <= 350.00.

Evidence in bundle: `12-worker-stall/report.txt`, `12-worker-stall/config.json`, `12-worker-stall/requests.jsonl`.

## worker-truncate

**FAIL**. Fault: `{"action": "cutoff", "bytes": 1024, "direction": "response", "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 66.48 (-33.52 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 66.48 (-33.52 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.84 | 17.55 (-1.6%) | 17.62 (-1.2%) |
| gap_p99_ms | 7.68 | 7.59 (-1.1%) | 7.64 (-0.5%) |
| goodput_rps | 6.02 | 3.97 (-34.1%) | 5.98 (-0.6%) |

All offered: **720**; completed: **660**; boundary/guard requests retained: **14**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

Failed checks:

- fault: completion %: 66.48; required >= 99.00.

Evidence in bundle: `13-worker-truncate/report.txt`, `13-worker-truncate/config.json`, `13-worker-truncate/requests.jsonl`.

## worker-unreachable

**PASS**. Fault: `{"action": "disable", "direction": "request", "seed": 42, "target": "worker-1"}`

| Metric | Healthy | Fault (change) | Recovery (change) |
|---|---:|---:|---:|
| completion_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| compliance_pct | 100.00 | 100.00 (+0.00 pp) | 100.00 (+0.00 pp) |
| ttft_p95_ms | 17.93 | 17.57 (-2.1%) | 17.04 (-5.0%) |
| gap_p99_ms | 7.68 | 7.68 (-0.1%) | 7.67 (-0.2%) |
| goodput_rps | 6.02 | 6.00 (-0.3%) | 5.98 (-0.6%) |

All offered: **720**; completed: **720**; boundary/guard requests retained: **13**.

Recovery stability window starts +0.00s after removal; confirmed +5.00s (30 requests). This is not node restart time.

All required checks passed.

Evidence in bundle: `14-worker-unreachable/report.txt`, `14-worker-unreachable/config.json`, `14-worker-unreachable/requests.jsonl`.

## Reproduction boundaries

The recipe, order, seed and report calculation are fixed. Regenerating from unchanged saved evidence with this source produces identical Markdown/JSON. Fresh TCP runs will have different timing samples because of CPU scheduling, buffering and TCP chunking; a seed does not make live networking deterministic. Compare distributions and verdicts, not bitwise-equal live timings. Each scenario here is one run, not a confidence interval.

The pre-fault deadline guard and control-boundary exclusions are explicit; those requests are still in all-offered totals. Inter-output events are not necessarily individual tokens. No natural failure likelihood, EC2 performance or GPU throughput is inferred.
