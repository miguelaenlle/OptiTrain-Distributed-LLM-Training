# Integration audit (in progress)

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

Evidence gates not yet passed. No exact cancellation or healthy parity result is
claimed until the compiled integration spike and preserved upstream checks pass.
