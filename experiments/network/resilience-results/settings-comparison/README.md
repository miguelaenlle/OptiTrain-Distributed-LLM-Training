# Controlled settings comparison and repeat

Both sides use the **same binaries**, timing profile, workload, fault seed 101, SLOs and acceptance criteria. Phases are explicitly shortened to **10/12/20s**, so this is a separate repeat, not a replacement for the full 120s matrix. Reference settings disable the new routing policies and retain the original 1s first-output timeout. Improved settings use the shipped resilience recipe.

| Scenario | Reference resilience | Improved resilience | Reference fault TTFT p95 | Improved fault TTFT p95 | Improved fault normal-SLO compliance |
|---|---|---|---:|---:|---:|
| baseline | PASS | PASS | 16.67ms | 16.93ms | 100.00% |
| worker-stall | FAIL | FAIL | 1017.04ms | 215.71ms | 97.18% |
| worker-flapping | FAIL | PASS | 1014.81ms | 215.13ms | 100.00% |

**The improved stalled-worker repeat still FAILED.** Recovery TTFT p95 was 19.925ms versus a 19.1807ms limit; recovery gap p99 was 10.444ms versus 8.9606ms; its qualifying window started at +7.75s versus the required +5s. All 252 requests completed, but fault-phase normal-SLO compliance was 97.18%, below the 99% normal-SLO target. The main full-duration stalled-worker run passed; this short repeat does not establish stable production performance. No threshold was changed to accept it.

[Reference raw evidence](reference.tar.gz) · [Improved-settings raw evidence](improved.tar.gz) · [Structured comparison](comparison.json)

Extract either archive into a fresh directory, then run `PYTHONPATH=src python -m orchestrator.nettest.suite --report-from DIRECTORY`. These repeats retain exit 1 for their valid failed contracts. Use the extracted `inputs/experiment.json` with `--scenarios baseline worker-stall worker-flapping --seed 101` to repeat the exact settings/workload with fresh measurements.
