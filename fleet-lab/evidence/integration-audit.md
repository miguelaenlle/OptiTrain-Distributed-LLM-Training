# Integration audit

Pinned upstream: a4053bc1161872420e1e0607cb3409ef659b828e.
ASTRA: d3469945c50410cac2e727c9ab627670c965db2a.

- Python `Scheduler.schedule` reserves KV and advances computed-token counters
  before execution. Only `add_done` commits valid completed computation/token
  production. A crash must destroy the scheduler/cache incarnation rather than
  credit its reserved computed counters.
- ASTRA `Workload` issues graph nodes through `Sys::register_event`; compute
  callbacks reside in the per-Sys queue. Analytical memory uses that same queue.
  Cancel must remove these callbacks and owned handler payloads, plus graph and
  hardware reservations. It must not wait for the old graph to finish.
- Shared `NetworkAnalytical::EventQueue` currently processes equal-time callbacks
  FIFO. The adapter needs stable phase ordering while preserving default FIFO for
  legacy runs. Zero-delay graph callbacks must settle before deadline/scheduling.
- Supported initial execution has TP=PP=1, no collectives, remote KV or PIM.
  Independent per-replica remote memory removes deferred shared-fabric effects.
- Legacy healthy CLI remains the parity reference. New controller protocol is
  versioned JSON lines, synchronized exclusively by ASTRA virtual nanoseconds.

The compiled cancellation spike passed: a crash at 1,000 ns removed the actual
pending compute callback; no old completion was observed; a fresh context completed
at 1,042,431,370 ns. The all-down clock advanced to future recovery. The focused
suite exercises equal-time crash priority, delayed notification, interrupted
streams, repeated faults during recovery/loading, colocated node loss, delayed
stale delivery suppression, original deadlines and cancellation ownership.

The original upstream validation passed all 58 checks and all eight recorded
`sim.csv`/summary comparisons were byte-identical. The opt-in healthy fixture
matched delivered-token counts and every ITL exactly; TTFT/E2E differed by only
the audited 1 ns upstream bootstrap graph. GitHub run 37256594058 independently
passed the fresh pinned build and the same gates on commit 607e08c.

ASAN/UBSAN passed the 15 real-engine tests. Leak detection is disabled for the
upstream process-lifetime Sys/logger ownership; the standalone clock test passed
with leak detection enabled. This does not establish upstream leak cleanliness.

A subsequent causal-identity audit found that request history used the current
worker incarnation when recording delayed observations. This affects diagnostic
labels, not callback guards, request outcomes, timing or billing. The correction and its regressions passed. A fresh 1,000-request retained
flagship audited every history/token event, reduced mislabels 178 to zero, and
matched all numerical metrics exactly under identical saved inputs. The frozen
sweep remains tied to its original revision; see the evidence ledger and
`attempt-identity-audit.json` for before/after provenance.
