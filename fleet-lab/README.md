# OptiTrain Fleet Lab

A CPU analytical simulator for request-serving fleets under node failures. The pinned LLMServingSim scheduler and KV implementation generate real profiled execution graphs. An authored C++ lifecycle reducer runs on ASTRA's event clock, cancels actual graph callbacks, and owns request identity, recovery, billing, metrics, indexed replay, candidate enumeration, analytic baselines and cost ranking. Python supplies the scheduler/KV frontend and local job API. React/TypeScript provides the experiment editor and replay dashboard.

This subsystem is opt-in and does not modify OptiTrain's existing training, Go router, Kubernetes, or real inference paths. No GPU or cloud provisioning is required.

## Quick start

Run from the repository root, with Docker, Git, Python 3 and Node 22+ installed:

```sh
python3 fleet-lab/scripts/bootstrap.py
# Only pinned analytical dependencies are fetched; ns3/HTSim are not required.
docker build -t optitrain-fleetlab:dev fleet-lab
docker run --rm -v "$PWD:/workspace" -w /workspace optitrain-fleetlab:dev bash fleet-lab/scripts/build.sh
npm ci --prefix fleet-lab/web
npm run build --prefix fleet-lab/web
docker run --rm -p 127.0.0.1:8765:8765 -v "$PWD:/workspace" -w /workspace \
  -e PYTHONPATH=/workspace/fleet-lab/python optitrain-fleetlab:dev \
  uvicorn fleetlab.api:app --host 0.0.0.0 --port 8765
```

Open <http://127.0.0.1:8765>. Run definitions are immutable; editing and pressing **Run from start** creates a new experiment. Playback is inspection, with physical state and delayed router observations shown separately. Choosing a replica for replay inspection does not change fault targets. Local API jobs are capped at two concurrent CPU processes. Canceling a job terminates its process group, including its active simulator.

## Calibrate, evaluate and search

```sh
# Replace the output name rather than overwriting an existing run.
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev python -m fleetlab.cli calibrate --output /workspace/fleet-lab/runs/calibration
# Use the highest measured passing q from calibration.json.
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev python -m fleetlab.cli search --q 10 --output /workspace/fleet-lab/runs/search
```

The default grid contains 152 nonempty integer `(regular, spot)` mixes with 1–16 replicas and two routing policies, or 304 candidates. Its required low-load suite includes healthy, half-fleet process failure, one spot termination, a correlated spot pool termination, and one regular process failure. A scenario with its required class absent is explicitly a no-op. `--suite healthy,half,single_spot,correlated_spot,regular_crash,all_down,half_high` includes all-down and high-load half-fleet cases in feasibility; the optimizer will not silently drop an impossible scenario.

Each candidate receives identical absolute arrivals and a shared observation/billing horizon across the selected suite. Low load is `0.4 × 16 × q`; high load is `0.75 × 16 × q`. The trace has 1,000 seeded requests with equally sampled prompts 128/256/1024 and outputs 32/64/128, and 25% eligible 128-token shared prefixes. `q` is the highest tested passing single-replica rate, not an asserted exact saturation boundary.

The search minimizes the maximum selected-scenario total modeled USD among candidates that pass every required scenario. Ties use worst TTFT, total replicas, regular replicas, then policy ID. C++ ranking also supports explicitly supplied normalized `scenario_weights`; no hidden probability weights are assumed. Analytic capacity/headroom/backlog/drain baselines are labeled heuristics, not SLO guarantees.

Exact optimizations preserve the objective:

- A completed failing scenario rejects the candidate; remaining scenarios are explicitly skipped.
- A monotone certificate can stop a run when even perfect future outcomes cannot meet the all-offered completion/compliance threshold. Its metrics are marked partial, its cost is **accrued** cost, and it cannot become a feasible contender.
- Homogeneous healthy/class-neutral half-failure dynamics can be reused across class splits when every nonbilling setting is identical and stochastic hazards are disabled. C++ recomputes billing from per-worker billed durations. Each reused result names its representative, equivalence key and verified provenance. Class-targeted faults are not cached.

Larger fleets are evaluated first. The default limits are 3,200 evaluated scenarios, 30 minutes and 20 GiB; the wall limit interrupts an active simulator. `coverage_complete` means every candidate was fully evaluated or exactly rejected. Invalid configurations, cache/provenance errors, interruption and incomplete coverage are never SLO failure certificates.

## Assumptions and controls

The fixed profile is RTX PRO 6000 Blackwell Server Edition, Llama 3.1 8B BF16, TP=PP=1, with one independent GPU replica per node by default. The hardware kernel profile is not a measured AWS/full-server profile. KV capacity follows the upstream full-model calculation at 90% memory utilization; activation/CUDA reserve and shared-fabric cancellation are outside this model. Contexts above the measured 16,384-token attention range are rejected.

Default assumed prices are USD 3/hour regular and 1.20/hour spot, with engineering provenance dated 2026-10-04 and EC2 Linux g7e.2xlarge/us-east-1 as a reference, not a measured price. Billing uses continuous per-second proration: allocated initialization, loading, idle/compute and process-failure time are billed; terminated replacement wait is not. A replacement allocation starts a new session. `minimum_billing_ns` optionally applies a per-session minimum, default zero. Lost work is already included in elapsed billing and is not charged again. Network/storage charges are excluded.

Default recovery is 60 seconds availability, 5 seconds initialization and 30 seconds loading; discovery and work-report delay are 5 seconds, with one-second report samples. Correlated spot availability is 120 seconds. Pre-first-delivered-token retries allow three attempts with 100 ms backoff and the original 60-second deadline. Interrupted streams fail. The global waiting and per-worker admission limits are 4,096.

The editor exposes failure time, exact targets/count, delays, prices and SLOs. Fixed targets/counts are for fixed-fleet runs. Search uses relative/pool scenario rules and rejects those fixed-target overrides. All-down and staggered presets are available. Advanced settings can be passed through the API `overrides` or CLI `--overrides settings.json`, including `delivery_delay_ns`, `minimum_billing_ns`, `max_attempts`, queue limits, grouped physical `placement`, and explicit seeded `stochastic_failures`. Assumed active-time exponential hazard means default to 24 hours spot and 1,000 hours regular; they are disabled unless explicitly enabled. Spot Advisor interruption bands are not converted into hourly hazards. Zero observation/delivery/backoff/recovery delays are valid idealized inputs; periodic report intervals, deadlines, limits and hazard means remain positive.

Completion must be at least 99%, and compliant requests at least 95% of all offered requests. A compliant request completes by its original deadline, has TTFT ≤ 1 second, and **every** ITL ≤ 100 ms. Rejection, failure, timeout and horizon truncation all reduce all-offered success. Latency distributions are explicitly conditional on completion, with separate all-delivered ITL diagnostics. Reports include TPOT/E2E, throughput/goodput with a declared window, queue/KV/cache/recompute peaks, lost scheduled work, recovery observations, arrival cohorts and requests already active at the fault.

## Artifacts and replay

A retained run has `manifest.json`, `events.jsonl`, the C++ event index and state snapshots in `events.jsonl.index.json`, `summary.json`, `metrics.json`, and provenance verification. The manifest includes upstream commits, profile hashes, authored source/patch hashes and binary hashes. Provenance is checked before and after execution, and across a search. Replay uses per-request/worker/type offsets and state snapshots; query filters do not alter reconstructed view state. Search retains every outcome/metric/manifest while discarding heavyweight graph/replay files. Flagship runs retain full token timing and replay.

`POST /api/runs`, `POST /api/search`, `POST /api/runs/{id}/cancel`, `GET /api/runs/{id}`, and `GET /api/runs/{id}/replay?until_ns=...&worker=...&request=...&event=...` power the dashboard. Run IDs are UUIDs and cannot address arbitrary files.

## Verification

```sh
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev pytest -q fleet-lab/tests/test_engine.py fleet-lab/tests/test_api.py
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev python fleet-lab/tests/healthy_parity.py
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev python fleet-lab/tests/oracle.py
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace/fleet-lab/python \
  optitrain-fleetlab:dev python fleet-lab/tests/demo.py
docker run --rm -v "$PWD:/workspace" -w /workspace optitrain-fleetlab:dev bash fleet-lab/scripts/sanitize.sh
```

The upstream `serving/validate.sh` is also required: 58 exact clock baselines and eight recorded per-request accuracy artifacts. ASAN/UBSAN checks exercise the real backend; upstream process-lifetime Sys/logger ownership requires disabling leak detection for that binary, while the standalone event-clock test runs with leak detection enabled. A small healthy parity fixture tolerates only the upstream bootstrap graph's 1 ns offset; wall-overhead results are scoped to that fixture and do not establish general performance.

See [verification evidence](evidence/README.md) for measured results, coverage and practical limits. Delta recompute/recall, advanced preemption and real-cluster validation remain separate roadmap work; this MVP makes no real GPU/failure-fidelity claim.
