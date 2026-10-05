# Fleet Lab verification evidence

The bounded grid covers **304 fleet/policy pairs**, with **zero feasible pairs**
under the unchanged default suite and SLOs. This is exact candidate coverage by
completed evaluations or monotone rejection certificates, not 1,520 simulated
scenario combinations. Healthy/half rejection short-circuited later scenarios.
There is no selected economic winner or savings claim.

## Revisions and provenance

- Frozen numerical sweep: `607e08c748d3f0979b38487f94e0e0eef127422a`.
  [Search manifest](search-manifest.json) records all source, profile and binary
  hashes. The primary and supplemental batches used this exact engine.
- Immutable attempt identity correction: `9eb6135785d18caf53fc1ab3552edcf62849627c`.
  [Retained re-audit](attempt-identity-audit.json) records before/after provenance,
  identical saved inputs, and identical hashes of **all numerical metrics**.
  The sweep is not relabeled as the diagnostic revision.
- UI preset correction: `b5b2119`. The editor now leaves replacement wait at its
  scenario preset unless explicitly overridden; correlated spot retains 120 s.
- Original flagship manifests have the same binary/profile hashes as the sweep,
  and their saved inputs exactly match the frozen factory's low/high experiments.
  Their authored hashes differ in API/CLI/scenario metadata code; preserve that
  distinction in [flagship provenance](flagship-provenance.json).
- Final PR-head CI is the acceptance record on [PR #20](https://github.com/miguelaenlle/OptiTrain-Distributed-LLM-Training/pull/20).
  The earlier independent fresh-run [CI run 37256594058](https://github.com/miguelaenlle/OptiTrain-Distributed-LLM-Training/actions/runs/37256594058)
  passed on the exact frozen sweep head.

## Search coverage and resource limits

[Coverage ledger](search-coverage.json) and [candidate certificates](search-candidates.json):

| Batch | Covered pairs | Evaluated scenarios | Completed physical executions | Exact reused results | Recorded runtime |
|---|---:|---:|---:|---:|---:|
| Primary | 217 | 434 | 34 | 400 | 1,800.59 s |
| Supplemental, new pairs | 87 | 164 | 26 | 138 | 477.72 s |
| Combined | 304 | 598 | 60 | 538 | 2,278.32 s |

One additional primary physical execution was interrupted and produced no valid
scenario result; it is excluded from the completed-execution counts above.

The primary batch remains **incomplete**: its active simulator was interrupted by
its 1,800-second cap. The separate supplemental container took 497.19 seconds
including startup/cache loading, so primary-recorded plus supplemental wall time
is 2,297.78 seconds (38.30 minutes). The proposed single-batch 30-minute target
was missed. No thresholds, workloads, failure definitions or primary caps were
changed to obtain coverage.

The combined evidence contains 304 healthy evaluations and 294 half-failure
evaluations; 922 remaining scenario evaluations were exactly skipped after
rejection. Default single-spot, correlated-spot and regular-crash constraints
remain selected: a failing healthy/half scenario already proves that candidate
cannot satisfy the whole suite. No scenario-specific full-grid result is claimed
for those skipped combinations. Partial metrics expose accrued cost and a
certificate; they are never ranked as a feasible full-window result.

Combined primary/supplemental artifacts use 5.31 GiB, below 20 GiB. Primary peak
child RSS was 387,140 KiB (378.1 MiB); supplemental was 324,824 KiB (317.2 MiB).
These are maximum individual-child RSS readings, not aggregate resident memory.
CPU jobs were bounded to two; diagnostic/held-out runs are separately recorded.

The clean, parameterized [continuation harness](../scripts/complete_search.py)
validates original provenance/primary evidence, preserves primary status, and
reports independent and combined budgets. The exact development harness used
for this numerical continuation is also [archived](supplemental-harness-used.py).
A focused real-engine test verifies the clean harness's full small-grid result,
reuse, primary immutability, budget exhaustion and provenance rejection.

## Calibration, flagships and held-out inputs

[Calibration](calibration.json): q = 10 requests/s, the highest **tested** passing
single-worker rate. Rates 5 and 10 achieved 100% completion/compliance. Rate 20
achieved 93.6% compliance with conditional TTFT p95 1.190 s. This is not an exact
saturation estimate. The grid uses a fixed absolute 64 requests/s trace.

The [unpruned flagship results](flagships.json) use 1,000 seeded mixed-length,
repeated-prefix requests, 8 regular + 8 spot replicas and 8 simultaneous failures:

| Load / policy | Completion | All-offered compliance | Conditional TTFT p95 | Assumed total USD |
|---|---:|---:|---:|---:|
| 64/s, round robin | 97.2% | 82.3% | 5.128 s | 1.6592 |
| 64/s, least observed work | 97.1% | 90.3% | 5.120 s | 1.6592 |
| 120/s, round robin | 94.3% | 68.8% | 5.149 s | 1.6254 |
| 120/s, least observed work | 93.6% | 75.1% | 5.144 s | 1.6254 |

Both policies fail the required targets. Least observed work improves compliance
in these traces while reducing completion, so this is not a universal policy
improvement. The all-down run adds a request after observed recovery: that probe
completed. Overall completion/compliance was 44.36% of 1,001 offered requests.

[Held-out results](heldout.json), [provenance](heldout-provenance.json): seeds 99
and 100, both policies, 1,000 requests each. Completion was 97.2–97.6% for round
robin and 96.4–97.4% for least observed work. No held-out tuning was performed.
Separate seed-99 least-work runs tested 0/10-second discovery and 5/120-second
replacement waits. Instant discovery raised compliance to 97.4%, but completion
remained 97.4% because interrupted streams fail. The engine regressions also
verify 30/120/600-second physical recovery and a fresh post-outage request.

[Independent baselines](baselines.json) record calibrated-q capacity, headroom,
fluid backlog/drain, assumed price ratios 0.2/0.4/0.6/0.8 and assumed 6/24/72-hour
spot active hazards. They are heuristics, not provider reliability or SLO
predictions. Fourteen replicas predict 70/s half-failure capacity versus 64/s
arrivals; sixteen predict 80/s. Replay still fails because interruptions,
discovery, routing and original deadlines are omitted by that fluid baseline.
Hazards are disabled in the default search. Prices are planning assumptions.

## Correctness and runtime gates

- Final authored suite: **23 passed**. It covers cancellation/ties, delayed
  notification, stream interruption, repeated recovery faults, colocated loss,
  seeded hazards, stale-delivery suppression, deadlines, billing, all-offered
  metrics, exact cache reuse/pruning, continuation and API cancellation races.
- Sanitized ASTRA backend: **20-test engine suite passed**, ASAN/UBSAN enabled.
  The metrics/ranking helper remains the release binary in this suite. Upstream
  process-lifetime Sys/logger ownership requires disabling backend leak checking;
  standalone event-clock ASAN/UBSAN runs with leak checking enabled and passed.
- Real cancellation spike: crash 1,000 ns, one actual callback removed, no old
  completion, fresh completion 1,042,431,370 ns; future all-down recovery advances
  the same ASTRA clock.
- Original upstream validation: **58/58**, plus **8/8** byte-identical `sim.csv`
  and summary artifacts. CI independently repeats these checks on a clean fetch.
- [Final healthy parity](healthy-parity-final.json): exact token counts and every
  ITL; all TTFT/E2E offsets are −1 ns, the audited upstream bootstrap resolution.
  One small fixture measured upstream 0.4659 s versus fleet 0.4637 s (−0.46%),
  including startup and retained replay. This meets the proposed 25% target for
  that sample, but does not establish general overhead. The [earlier sample](healthy-parity.json)
  and both revisions remain separately recorded.
- [Final bounded oracle](oracle-final.json): optimized search agrees with an
  independently exhaustive ten-candidate grid in both a positive case (ten
  feasible candidates) and a failure case (zero). Tests also compare randomized
  C++ metrics against an independent Python reducer for 100 seeded fixtures.
- [Replay benchmark](replay-benchmark.json): four indexed queries at 0/5/10/170 s
  took 0.403/0.436/0.479/0.426 s including process startup, index load and JSON
  transfer. Each scanned at most 4,748 state events. The original retained low
  flagship journal was 80.21 MiB and index 35.59 MiB; this is a four-query sample.
- [Attempt identity audit](attempt-identity-audit.json): 146,812 history events and
  72,154 tokens checked. Mislabeled history fell 178 → 0; invalid-incarnation
  tokens were zero before and after. Twenty-nine delayed observations explicitly
  distinguish old attempt identity from current worker identity. All numerical
  metrics are exactly equal across the saved-input re-run.

## Retained-run performance

[Measured performance](retained-performance.json) records one fresh 1,000-request
run using identical saved flagship inputs. The full run, including graph generation,
backend execution, indexing and metrics, took **51.678 s**. Measured CPU time was
18.696 s user + 3.079 s system in the parent and 10.650 s user + 1.132 s system in
completed children. Parent and maximum child process-lifetime peak RSS were both
534,584 KiB (522.1 MiB); these are separate maxima, not aggregate memory usage.
The journal contains 329,863 events (**6,383 events per wall second**). Retained
artifacts before the benchmark report total 342,634,179 bytes (326.8 MiB) across
17,685 files, with per-path sizes recorded in the JSON. The 177.772 s virtual
horizon yields **3.44 virtual seconds per wall second**: analytical virtual-clock
throughput, not measured GPU or service speedup. All numerical metrics remain
exactly equal to the original saved-input flagship. This is a single retained-run
sample rather than a general performance guarantee.

## Dashboard checks

[UI ledger](ui-smoke.json) and genuine [desktop](screenshots/desktop.jpg),
[mobile](screenshots/mobile.jpg), [cancellation](screenshots/canceled.jpg) captures.
Saved history, playback, seek, physical versus observed health, KV reset, request
inspection, event filtering, suite selection and fixed-target validation were
exercised. An edited real job persisted its inputs and canceled its process group.
A correlated-spot default job preserved the 120-second wait. Browser console
errors were absent. Mobile history remains usable; viewport zoom and actual CSS
width are recorded in the ledger. Application copy uses consistent professional
wording, descriptive scenario labels, and readable status and event names. Scenario
labels preserve their original API identifiers. TypeScript/Vite build and npm audit passed.

## Reproduction and practical limits

Use the subsystem [README](../README.md) for the pinned CPU build and fresh output
directories. The frozen sweep is reproduced with the frozen engine revision;
bring the checked-in continuation harness into that checkout before continuing.
The harness rejects another revision's binaries or profiles rather than mixing
them. Run the final identity comparison against a retained before-run:

```sh
python fleet-lab/scripts/audit_attempt_identity.py \
  --before /workspace/.cache/flagships/low-round_robin \
  --output /workspace/.cache/identity-flagship-fresh
```

Capture another retained-run performance sample from saved inputs in a fresh directory:

```sh
python fleet-lab/scripts/benchmark_run.py \
  --inputs /workspace/.cache/flagships/low-round_robin \
  --output /workspace/.cache/performance-flagship-fresh
```

This is CPU analytical evidence under fixed kernel profiles, not empirical GPU,
AWS server, outage or billing fidelity. TP=PP=1 with independent memory is the
supported initial configuration. Search orchestration/budgets/equivalence-cache
transport live in Python; C++ owns the clock/lifecycle, certificates, billing,
metrics/replay, enumeration and ranking. It does not claim that the orchestration
layer was ported to C++. Delta recall, advanced preemption and real-cluster
validation remain separate roadmap work. No hardware was provisioned or PR merged.
