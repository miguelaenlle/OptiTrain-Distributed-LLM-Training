# Recorded network scenarios

[Full generated report](report.md) · [Recorded evidence](evidence.tar.gz) ·
[Offline reproduction checks](reproduction.json) ·
[Pinned workload and acceptance criteria](../report.json)

The report measures all 14 current presets against the same three-worker,
H100-derived CPU replay configuration. These are controlled TCP impairments,
not measured GPU inference, EC2 networking, or natural failure probabilities.
Worker registrations stay live throughout the test. Recovery means clearing
the network rule and measuring stability, not restarting/replacing a node or
testing Kubernetes reconciliation.

**Recorded result:** 14 valid runs, 10,080 offered requests; **6 PASS / 8 FAIL /
0 INVALID** under the unchanged contract. All runs used measurement source
`28ff966fc55289ae1128d46081016ebd604bd413`, seed 42, Linux arm64 and Python 3.12.3.

## Findings

- **Refusal versus hanging matters.** The unreachable preset refuses new TCP
  connections: failover kept TTFT p95 at 17.57ms. A stalled worker still completed
  requests, but TTFT p95 reached 1,016.79ms. The configured 1s first-output timeout
  already exceeds the 600ms fault TTFT budget before a retry. Timeout tuning is a
  useful next experiment; this does not establish a safe production timeout.
- **Completion alone hides degraded streaming.** Jitter and the download cap
  completed all requests, but gap p99 reached 92.28ms and 101.37ms. Their
  fault-phase normal-SLO compliance was 88.83% and 32.96%, respectively.
- **The delay allowance is explicit.** A 300ms worker delay produced 320.56ms
  TTFT p95 and 66.48% normal-SLO compliance, meeting the declared ≥65% fault
  allowance. Two 100ms response-hop delays produced 218.48ms TTFT p95 and met
  the normal 250ms TTFT target.
- **Interruptions remain failures.** Truncation lost 60 responses; deliberate
  client cancellation interrupted 180. No scenario retried after output was
  committed. Successful connection failover does not imply mid-stream recovery.
- **Outages and uploads consume request deadlines.** Only 11/179 fault-cohort
  requests completed in the all-worker stall run, and 10/179 under
  the upload cap; late survivors can complete after rule removal. Their latency
  percentiles must be read alongside these completion denominators.
- **The current idle preset is not a stressor for this profile.** Its 50ms
  idle rule caused zero expirations with roughly 6ms output pacing. This PASS
  means active traffic was unaffected; the separate quiet-connection test
  exercises actual idle expiry.

The initial matrix found a proxy idle-timer defect that held active bytes until
the timer fired. A socket regression reproduced it, the proxy was fixed, and
**all 14 scenarios were rerun** from the corrected commit for this dataset.
No acceptance threshold was loosened. Linux CI passed all 37 Python network
tests plus Go race tests and C++ sanitizers.

## Run the experiment again

From the repository root, with the prerequisites in
[network testing](../../../docs/network-testing.md) installed:

```sh
PYTHONPATH=src python -m orchestrator.nettest.suite
```

Allow about 28 minutes plus compilation. Read `.fleet/network-report/report.md`.
The suite completes every scenario even when a scenario fails its SLO contract.
Exit **1** is expected when valid failure scenarios miss the frozen budgets;
**2** means invalid/incomplete evidence. Use a new `--output` directory for a
fresh run. No GPU, model download or cloud access is needed.

## Reproduce these exact recorded findings offline

```sh
mkdir -p .fleet/recorded-network-report
tar -xzf experiments/network/results/evidence.tar.gz -C .fleet/recorded-network-report
PYTHONPATH=src python -m orchestrator.nettest.suite \
  --report-from .fleet/recorded-network-report
```

This starts no traffic and requires no compiler. It verifies the evidence
checksums, recomputes metrics from raw requests/attempts, checks them against the
original reports, and recreates `report.md` and `summary.json`. Each case's
`report.txt` includes every required check, actual value and threshold. The
archive contains no executables or model weights. Case file paths in the report
refer to the extracted archive.

The complete dataset was regenerated on Linux/Python 3.12.3 with networking
disabled and no compiler/serving binaries installed, and on macOS/Python 3.14.7.
Markdown, JSON and archive SHA-256 hashes matched the originals on both. See
`reproduction.json` for the exact hashes and retained exit-code meaning.

The recipe, seed, order and report calculation are fixed. Live TCP measurements
vary with the machine and scheduling; only regeneration from the same recorded
evidence is expected to be byte-for-byte reproducible in the same runtime.
Tables round to two decimal places; raw JSON and the per-case check logs retain
the full recorded precision.

## Frozen acceptance contract

| Metric | Healthy | Fault | Recovery |
|---|---|---|---|
| Completion | ≥99% | ≥99% | ≥99% |
| Normal-SLO compliance | ≥99% | ≥65%, drop ≤35 percentage points | ≥99% |
| SLO goodput | Reference | ≥65% of healthy | ≥95% of healthy |
| TTFT p95 | Normal SLO ≤250ms | ≤600ms and added ≤350ms | ≤110% of healthy |
| Output-gap p99 | Normal per-output gap ≤100ms | ≤50ms and ≤125% of healthy | ≤110% of healthy |
| Stability window | — | — | Starts within 5s, holds 5s, at least 20 requests |

Normal SLO also requires completion and total request time ≤2s. No generator
drops, at most three attempts and no retries after output commitment are
required. Latency percentiles describe received output; failed requests remain
in completion/SLO denominators. Requests dispatched during a fault can finish
after removal and still belong to the fault cohort. Guard/boundary requests are
shown separately and retained in overall totals.
