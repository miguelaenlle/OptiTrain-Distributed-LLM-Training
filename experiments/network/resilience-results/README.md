# Resilience results and before/after comparison

[Full generated report](report.md) · [Raw evidence](evidence.tar.gz) · [Machine-readable comparison](comparison.json) · [Contracts](../../../docs/network-resilience-contracts.md)

CPU-only Linux arm64 replay: 3 workers, 6 RPS, 512 input / 16 output events, 30/30/60s phases, seed 42. The H100-derived schedule is an estimate, not measured GPU serving. The workload, timing profile, normal SLO and original acceptance thresholds match the [pre-change experiment](../results/README.md).

**The original-contract columns use the same acceptance checks.** The resilience column uses the separately versioned failure-handling contract. A green resilience result does not erase incomplete requests or normal-SLO violations. The idle scenario is explicitly stronger than before.

| Scenario | Original contract before | Original contract after | Resilience after | Normal SLO after |
|---|---|---|---|---|
| baseline | PASS | PASS | PASS | PASS |
| client-delay | PASS | PASS | PASS | PASS |
| client-disconnect | FAIL | FAIL | PASS | FAIL |
| client-jitter | FAIL | FAIL | PASS | FAIL |
| combined-path-delay | PASS | PASS | PASS | PASS |
| fleet-stall | FAIL | FAIL | PASS | FAIL |
| idle-expiry | PASS | FAIL | PASS | FAIL |
| slow-client-download | FAIL | FAIL | PASS | FAIL |
| slow-client-upload | FAIL | FAIL | PASS | FAIL |
| worker-delay | PASS | PASS | PASS | PASS |
| worker-flapping | FAIL | PASS | PASS | PASS |
| worker-stall | FAIL | PASS | PASS | PASS |
| worker-truncate | FAIL | FAIL | PASS | FAIL |
| worker-unreachable | PASS | PASS | PASS | PASS |

**Final totals:** 14/14 resilience PASS, 7/14 original-contract PASS, 7/14 normal-SLO PASS; 10,080 offered and 9,507 completed requests. No generator drops, deadline overruns or retries after committed output. The original dataset had 6/14 original-contract passes. Two genuine passes were gained (stall/flapping); the newly activated idle-expiry test now intentionally loses partial responses and fails the original completion target.

**Repeat limitation:** the [controlled settings repeat](settings-comparison/README.md) confirmed the ~1,017ms →216ms fault-latency gain, but the short improved stall run still **failed recovery checks** and reached only 97.18% fault-phase normal-SLO compliance. The full matrix passing does not establish universal stability.

## Measured changes

- **worker-delay:** TTFT p95 320.56 → 214.88ms; normal-SLO compliance 66.48% → 100.00%. Completion 100.00%.
- **worker-stall:** TTFT p95 1016.79 → 214.63ms; normal-SLO compliance 67.22% → 100.00%. Completion 100.00%.
- **worker-flapping:** TTFT p95 1015.43 → 214.92ms; normal-SLO compliance 86.67% → 100.00%. Completion 100.00%.

Truncation lost 21 responses versus 60 before: quarantining the failing worker protects later requests, while already-committed streams still cannot resume.

Read the original and normal-SLO verdicts for availability. Cancellations, total outages and truncated streams still lose requests. Truncation has no transparent resume protocol. Client bandwidth/jitter still affect received output. Recovery means post-rule-removal traffic stability, not instance restart.

## Reproduce

```sh
PYTHONPATH=src python -m orchestrator.nettest.suite --config experiments/network/resilience.json --output .fleet/resilience-report
# Offline: no serving binaries, GPU or traffic required.
mkdir -p .fleet/recorded-resilience
tar -xzf experiments/network/resilience-results/evidence.tar.gz -C .fleet/recorded-resilience
PYTHONPATH=src python -m orchestrator.nettest.suite --report-from .fleet/recorded-resilience
```

The 14-case live run takes approximately 28 minutes plus build. Use a new output directory. Live timings vary; saved-evidence regeneration verifies hashes and rederives budgets/metrics from the frozen profile, requests, attempts and worker/proxy events.

## Verification and retained trials

The documented 32-second quick command passed natively on macOS after rebuilding the final binaries. Linux CI passed CTest, both Go race suites, all 40 network Python tests, and address/undefined-behavior/leak sanitizers. [Offline reproduction hashes](reproduction.json) matched for report Markdown, summary JSON and archive on macOS/Python 3.14 and Linux/Python 3.12, with networking disabled and no installed serving binaries/compiler in the Linux proof.

[Unsuccessful trials and short controls](trials/README.md) remain available, including the too-aggressive 100ms configuration and the all-worker-quarantine recovery defect. No normal SLO or original acceptance threshold was loosened. Final live measurement files were kept inside Linux and copied out after completion; earlier host-mounted controls showed timing interference. The historical before/after timings therefore should not be interpreted as a microbenchmark of router overhead.
