# Network-test verification

Executed locally on macOS arm64 and Ubuntu 24.04 arm64 containers. These are
observations, not guaranteed performance or hardware-fidelity claims. The
test runner retains resolved inputs, binary/profile hashes, request/attempt
JSONL, fault events and logs under `.fleet/net-tests/`.

## Checks executed

These initial implementation checks preceded the complete preset matrix. The
current automated suite has **37 Python cases** after adding matrix/offline
regeneration coverage and an active-traffic idle-timer regression; Linux CI
passed them with sanitizers. See the [complete scenario findings](../experiments/network/results/README.md)
and its checksummed, portable evidence bundle for the current configuration.

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

### Resilience contracts and opt-in routing

The versioned [resilience recipe](network-resilience-contracts.md) preserves the
original acceptance checks and reports them separately from scenario-specific
failure handling and normal serving SLOs. The original dataset is unchanged;
offline regeneration with the updated evaluator reproduced its Markdown and
archive hashes exactly.

Linux CI at `dfee96b` passed CTest, both complete Go race suites, all **40 Python
network tests**, and address/undefined-behavior/leak sanitizers. Native Go race
tests and report-accounting tests also passed. No GPU or cloud was exercised.

Controls caught two actual configuration/recovery defects: a 100ms timeout
rejected the combined 100ms worker-link delay, and total worker quarantine
extended failed service into recovery after a slow-upload burst. The final
recipe uses 200ms, two failures before quarantine, and one global foreground
probe when all workers are quarantined. The probe is concurrency bounded and
does not permit retries after output commitment. Production routing defaults
remain unchanged; these settings are explicit replay experiments.

The isolated 10/12/20s recovery-probe controls passed baseline, whole-fleet
outage handling and slow-upload recovery. Combined delay completed every
request within the normal SLO but failed its retained relative-gap check
(9.568ms observed, 9.51125ms limit). Earlier runs also showed healthy timing
interference. [Raw unsuccessful trials and controls](../experiments/network/resilience-results/trials/README.md)
are retained; none was relabeled as a success or used to loosen a threshold.

The [final full matrix](../experiments/network/resilience-results/README.md) passed all 14 resilience contracts: 10,080 offered, 9,507 completed, zero generator drops, zero deadline overruns and zero retries after output commitment. Seven scenarios still breach the normal serving SLO. Under the unchanged original acceptance checks, stall/flapping improved from FAIL to PASS; idle expiry changed to FAIL because the strengthened fixture now actually expires. Fault TTFT p95 was 214.627ms for stall and 214.920ms for flapping, versus 1016.790ms and 1015.430ms in the original dataset.

The rebuilt native quick command passed. Extracted evidence regenerated byte-identical Markdown, JSON and archive hashes on macOS/Python 3.14 and Linux/Python 3.12 without original run paths, network access or installed serving binaries/compiler in the Linux proof.

A separate [same-binary settings comparison](../experiments/network/resilience-results/settings-comparison/README.md) used seed 101 and explicit 10/12/20s phases. It confirmed stalled-worker fault TTFT p95 improving from 1017.040ms to 215.707ms, but the improved stalled-worker repeat still FAILED recovery checks (qualifying window +7.75s, required ≤5s) and had 97.18% fault-phase normal-SLO compliance. Its failed checks and raw evidence remain published. The repeat therefore does not establish universally stable SLO performance. All workers received traffic again in recovery in every full-matrix case, and client cancellation produced zero breaker-open events; see the saved recovery evidence.

New regression coverage is deliberately small:

| Test | What it protects |
|---|---|
| `router-go/stream_pool_test.go::TestStreamBreakerRejectsStaleSuccessAndAllowsOneProbe` | A concurrent old success cannot clear a newer failure; only one half-open probe reserves a worker. |
| `router-go/stream_pool_test.go::TestStreamClientFailureDoesNotOpenBreakerAndRetriesAvoidBusyWorker` | Client faults stay neutral, retries avoid busy destinations, retired addresses lose their health state. |
| `router-go/stream_pool_test.go::TestStreamTotalQuarantineHasOnlyOneImmediateRecoveryProbe` | At most one global probe bypasses cooldown, then a responsive worker immediately returns to service. |
| `router-go/stream_test.go::TestStreamDownstreamFailureDoesNotQuarantineWorker` | A real upstream plus a failed downstream writer cannot blacklist a healthy worker. |
| `tests/nettest/test_report.py::test_cancellation_contract_keeps_slo_failure_and_requires_worker_evidence` | A cancellation resilience PASS retains the SLO failure and requires proof that work stopped. |
| `tests/nettest/test_report.py::test_degraded_bandwidth_budget_comes_from_inputs_and_accounts_for_every_gap` | The public budget comes from configured bandwidth/profile inputs; a single over-budget gap still fails. |
| `tests/nettest/test_suite.py::test_resilience_suite_cancellation_idle_expiry_and_offline_evidence` | Real cancellation, actual idle-triggered closure, all components, and deterministic/tamper-evident report rebuilding. |

The concurrency tests protect state transitions that are difficult to assess
from sequential examples. The full-path test checks their composition with
actual sockets and evidence; it does not duplicate each transport unit case.
The existing strict-SLO negative test still requires a genuine FAIL.

### Existing transport and serving checks

| File | Behavior protected |
|---|---|
| `netfault/tests/unit.cpp` | Rule parsing/bounds and endpoint validation |
| `tests/nettest/test_faults.py` | Byte integrity/half-close, directional delay, active stall clear/expiry, exact cutoff, rate limit, jitter order, idle expiry, disable/re-enable, rejected control rules |
| `tests/nettest/test_replay.py` | Output precedes completion, unsupported shapes fail, canceled work releases the worker |
| `router-go/stream_test.go` | Immediate flush, retry before output, no retry after output, cancellation, malformed SSE, first-output deadline despite headers |
| `loadgen/stream_test.go` | Actual SSE timing, incomplete streams, event versus read-chunk parsing, buffered body latency, bounded capacity/all-offered accounting, cancellation window |
| `tests/nettest/test_report.py` | Failure/drop denominators, per-gap SLOs, explicit percentiles, missing evidence, failed contracts, invalid inputs |
| `tests/nettest/test_runtime.py` | Controller-loss cleanup, readable evidence after stop, evidence path containment |
| `tests/nettest/test_suite.py` | A failed scenario does not stop the next one; Markdown/JSON/archive regeneration is identical; extracted evidence works without binaries or original paths; changed evidence is rejected |
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
