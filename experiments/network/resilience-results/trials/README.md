# Retained unsuccessful trials and controls

These are earlier measurements, not the final matrix. The archives preserve failed contracts and interrupted/not-run cases. They use the same normal SLOs and original acceptance thresholds. Short controls use explicit 10/12/20s phases, not the final 30/30/60s phases.

| Trial | Resilience results | Interpretation |
|---|---|---|
| [100ms-initial](100ms-initial.tar.gz) | 7 PASS, 3 FAIL, 1 INVALID, 3 NOT RUN | 100ms timeout / threshold 1. Rejected the combined 100ms worker delay; also failed healthy/recovery checks. Stopped after the deterministic regression was established; unfinished cases stay INVALID/NOT RUN. |
| [200ms-controls](200ms-controls.tar.gz) | 4 FAIL | 200ms / threshold 2. Healthy controls showed scheduling interference (including 200–300ms output gaps). No SLO was relaxed. |
| [200ms-isolated](200ms-isolated.tar.gz) | 2 PASS, 2 FAIL | Logs inside Linux and idle sleep inhibited. Baseline/fleet outage passed; combined delay missed its relative-gap limit by 0.28ms despite full normal-SLO compliance. Slow-upload recovery still failed while all workers were quarantined. |
| [recovery-probe-controls](recovery-probe-controls.tar.gz) | 3 PASS, 1 FAIL | Same settings plus one bounded probe on total quarantine. Slow-upload recovery passed. Combined delay retained a relative-gap failure; all requests completed and met the normal SLO. |

Extract an archive into a new directory and run `PYTHONPATH=src python -m orchestrator.nettest.suite --report-from DIRECTORY`. This starts no traffic; failed trials retain nonzero exit status. Read `suite.json` for the exact source, settings, profile, binaries and per-case hashes.

The initial trial also recorded healthy timing interference; no provider reliability or production timeout recommendation is inferred. Isolation reduced interference in subsequent controls, but these runs do not establish its root cause.
