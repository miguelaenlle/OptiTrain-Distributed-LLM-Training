# Network-test verification

Executed locally on macOS arm64 and Ubuntu 24.04 arm64 containers. These are
observations, not guaranteed performance or hardware-fidelity claims. The
test runner retains resolved inputs, binary/profile hashes, request/attempt
JSONL, fault events and logs under `.fleet/net-tests/`.

## Checks executed

| Check | Result |
|---|---|
| Native C++ CTest | PASS |
| Native socket/replay tests with address + undefined-behavior sanitizers | 14 passed; macOS leak detection unsupported, so disabled there |
| Full Linux CTest + address/undefined-behavior/leak sanitizers | PASS; 35 Python tests passed, both Go race suites passed |
| Go router and load generator, including existing tests | Both full `go test -race` suites passed |
| New Python suite, including two full-path integration tests | 35 passed |
| Existing Python inference/router/registry/metrics/discovery/preemption/API tests | 52 passed; one dependency deprecation warning |
| NVIDIA AIConfigurator 0.12.0 export | Actual SDK export succeeded on CPU; prepared H100 profile checked in |
| Native real CPU inference | Pinned 491MB Qwen 0.5B Q4_K_M, llama.cpp b11976, GPU offload disabled; full 120s test PASS, 60/60 completed |
| Linux package + SSH/SCP, one host | PASS; all 84 offered requests completed |
| Three separate Linux containers over native TCP, controlled over SSH | Proxied baseline and direct baseline PASS, 84/84 completed each; worker-delay completed 84/84 and exposed the expected TTFT increase, but failed the short-run goodput contract |
| Unavailable worker through the full native serving path | PASS; 168/168 completed, at most two attempts, no retry after committed output |
| Truncated worker stream and client-disconnect presets | Correctly returned FAIL for the availability contract, with interrupted requests retained and no retry after committed output |
| Python lint/format, C++ format, whitespace checks | PASS |

Linux socket tests use real processes and connections. CTest alone covers rule
and endpoint validation; it does not establish transport correctness. Sanitizer
runs inspect process logs as well as exit status. GitHub CI builds all binaries
before the socket/integration tests and retains their evidence.

## Default replay result

Run `nettest-3b2ca67c6f62`: three workers, 6 RPS, 30/30/60s phases,
H100-derived batch-one timing schedule, +300ms worker-1 response delay.
**PASS under the documented unchanged criteria; 720/720 completed, no generator
drops.** Thirteen guard/control-boundary requests remain in overall totals.

| Metric | Healthy | Fault | After removal |
|---|---:|---:|---:|
| Normal-SLO compliance | 100% | 66.67% | 100% |
| TTFT p95 | 15.54ms | 316.16ms | 15.40ms |
| Output-gap p99 | 6.94ms | 7.06ms | 6.96ms |
| SLO goodput | 6.02 RPS | 4.00 RPS | 5.98 RPS |

The fault deliberately makes approximately one third of requests miss the
250ms normal TTFT SLO. The fault allowance accepts this explicitly; it does
not relabel those requests as SLO-compliant. A constant network delay shifts
TTFT while largely preserving subsequent gaps. The recovery qualification
window starts at removal and is confirmed five seconds later; this is not a
measurement of restarting a node.

Replay is an application-level timed surrogate, not measured H100 serving.
It does not establish continuous-batching/KV-cache accuracy or GPU throughput.

Full real-CPU run `nettest-989725dbebdd` also passed: 60/60 completed at 0.5 RPS,
TTFT p95 17.78 → 319.43 → 18.30ms and output-gap p99
10.69 → 10.73 → 10.99ms. It uses the separate CPU budgets in the resolved config,
including a 5s first-output SLO, so all requests met that broader normal SLO.

## Failed contracts retained

- The negative integration test requires ≥99% normal-SLO compliance during the
  same worker delay. The experiment returns **FAIL**, and the test verifies that
  failure plus the measured TTFT increase. No threshold is changed to pass it.
- Three-host short worker-delay smoke `optitrain-nettest-ssh-f03f1881-2` returns
  **FAIL** for fault/recovery goodput: 3.76/5.85 RPS versus required 4.10/6.00.
  All 84 requests completed; TTFT p95 rose 17.37 → 318.84ms. Its short, uneven
  dispatch cohorts make rate comparisons sensitive to arrival alignment. The
  SSH smoke reports functional completion separately from this failed contract.
- Earlier short CPU-model run `nettest-9a447270e43b` completed 25/25 requests but
  **FAIL**ed recovery goodput and latency ratios: recovery TTFT p95 169.95ms
  versus healthy 13.82ms. It is not discarded or presented as a passing result.
  The full default CPU run has more samples but still is only functional
  coverage; low sample counts do not establish production p99 availability.

Testing found and fixed a real Go load-generator startup race (a request could
be falsely dropped before a worker goroutine received it), baseline
contamination by requests still in flight at activation, and incorrect
cross-host addressing in the optional direct replay baseline. No production
acceptance budget was loosened to address these defects.

## Coverage map

| File | Behavior protected |
|---|---|
| `netfault/tests/unit.cpp` | Rule parsing/bounds and endpoint validation |
| `tests/nettest/test_faults.py` | Byte integrity/half-close, directional delay, active stall clear/expiry, exact cutoff, rate limit, jitter order, idle expiry, disable/re-enable, rejected control rules |
| `tests/nettest/test_replay.py` | Output precedes completion, unsupported shapes fail, canceled work releases the worker |
| `router-go/stream_test.go` | Immediate flush, retry before output, no retry after output, cancellation, malformed SSE, first-output deadline despite headers |
| `loadgen/stream_test.go` | Actual SSE timing, incomplete streams, event versus read-chunk parsing, buffered body latency, bounded capacity/all-offered accounting, cancellation window |
| `tests/nettest/test_report.py` | Failure/drop denominators, per-gap SLOs, explicit percentiles, missing evidence, failed contracts, invalid inputs |
| `tests/nettest/test_runtime.py` | Controller-loss cleanup, readable evidence after stop, evidence path containment |
| `tests/nettest/test_profile.py` | Prefill counted once, growing decode context, bad estimates fail without substitution |
| `tests/nettest/test_end_to_end.py` | All actual components and persisted evidence; strict SLO fails with an injected delay |

Socket tests isolate transport mechanics; Go tests exercise streaming semantics;
the two integration tests check their composition. This overlap is deliberate.

## Boundaries

No EC2 deployment, real GPU run, NCSA Delta run, Internet failure-rate study,
packet-loss simulation or GPU-capacity claim was performed. SSH containers
verify packaging, cross-host addresses and cleanup, not AWS security groups or
VPC behavior. Repeat longer runs on the target environment with declared SLOs
before using the results for an availability decision.
