import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
type Event = {
  event: string;
  time_ns: number;
  seq: number;
  worker?: number;
  id?: number;
  state?: string;
  delivered?: number;
  incarnation?: number;
  observed_ready?: boolean;
  value?: Record<string, number>;
};
type Run = {
  id: string;
  state: string;
  manifest?: {
    horizon_ns: number;
    regular: number;
    spot: number;
    policy: string;
    scenario: string;
    price_provenance: Record<string, unknown>;
    provenance: Record<string, unknown>;
  };
  metrics?: Record<string, any>;
  search?: Record<string, any>;
  progress?: Record<string, any>;
};
type Replay = {
  workers: Record<string, Event & { stats?: Record<string, number> }>;
  requests: Record<string, Event>;
  events: Event[];
};
const scenarioLabels: Record<string, string> = {
  healthy: "Normal operation",
  single_spot: "Single spot replica failure",
  half: "50% fleet failure",
  correlated_spot: "Correlated spot failures",
  regular_crash: "Regular replica failure",
  staggered: "Staggered failures",
  all_down: "Complete fleet outage",
  half_high: "50% fleet failure at high load",
};
const displayLabel = (value: string) => {
  const label = value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
};
const API = import.meta.env?.VITE_API ?? "";
async function api(path: string, body?: unknown) {
  const response = await fetch(
    API + "/api" + path,
    body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {},
  );
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
const seconds = (n: number) => (n / 1e9).toFixed(3);
const pct = (n: number | undefined) =>
  n == null ? "—" : (n * 100).toFixed(1) + "%";
function Histogram({
  title,
  data,
}: {
  title: string;
  data?: {
    samples: number;
    histogram?: { from_ns: number; to_ns: number | null; count: number }[];
  };
}) {
  const bins = data?.histogram ?? [];
  const max = Math.max(1, ...bins.map((b) => b.count));
  return (
    <article className="histogram">
      <h3>{title}</h3>
      <small>{data?.samples ?? 0} samples · hover for bin details</small>
      <svg role="img" aria-label={title} viewBox="0 0 420 150">
        {bins.map((b, i) => (
          <g key={i}>
            <rect
              x={10 + i * 50}
              y={120 - (b.count / max) * 90}
              width={34}
              height={(b.count / max) * 90}
              rx={3}
            >
              <title>
                {(b.from_ns / 1e6).toFixed(0)}–
                {b.to_ns == null ? "∞" : (b.to_ns / 1e6).toFixed(0)} ms:{" "}
                {b.count} samples
              </title>
            </rect>
            <text x={10 + i * 50} y={140}>
              {b.from_ns >= 1e9
                ? b.from_ns / 1e9 + "s"
                : b.from_ns / 1e6 + "ms"}
            </text>
          </g>
        ))}
      </svg>
    </article>
  );
}
function App() {
  const [runs, setRuns] = useState<Run[]>([]),
    [selected, setSelected] = useState(""),
    [run, setRun] = useState<Run | null>(null),
    [replay, setReplay] = useState<Replay | null>(null);
  const [regular, setRegular] = useState(8),
    [spot, setSpot] = useState(8),
    [policy, setPolicy] = useState("round_robin"),
    [scenario, setScenario] = useState("half"),
    [rate, setRate] = useState(64),
    [seed, setSeed] = useState(42),
    [q, setQ] = useState(10);
  const [settings, setSettings] = useState({
    availability: -1,
    init: 5,
    load: 30,
    discovery: 5,
    delivery: 0,
    regularPrice: 3,
    spotPrice: 1.2,
    completion: 99,
    compliance: 95,
    ttft: 1,
    itl: 100,
    faultTime: -1,
    faultCount: 0,
  });
  const [targets, setTargets] = useState(""),
    [suite, setSuite] = useState([
      "healthy",
      "half",
      "single_spot",
      "correlated_spot",
      "regular_crash",
    ]),
    [inspect, setInspect] = useState("");
  const setSetting = (key: keyof typeof settings, value: number) =>
    setSettings((s) => ({ ...s, [key]: value }));
  const overrides = {
    ...(settings.availability >= 0
      ? { availability_ns: Math.round(settings.availability * 1e9) }
      : {}),
    init_ns: Math.round(settings.init * 1e9),
    load_ns: Math.round(settings.load * 1e9),
    discovery_ns: Math.round(settings.discovery * 1e9),
    delivery_delay_ns: Math.round(settings.delivery * 1e9),
    prices: { regular: settings.regularPrice, spot: settings.spotPrice },
    completion_threshold: settings.completion / 100,
    compliance_threshold: settings.compliance / 100,
    ttft_limit_ns: Math.round(settings.ttft * 1e9),
    itl_limit_ns: Math.round(settings.itl * 1e6),
    ...(settings.faultTime >= 0
      ? { fault_time_ns: Math.round(settings.faultTime * 1e9) }
      : {}),
    ...(settings.faultCount > 0 ? { fault_count: settings.faultCount } : {}),
    ...(targets.trim()
      ? { fault_targets: targets.split(",").map((t) => Number(t.trim())) }
      : {}),
  };
  const [cursor, setCursor] = useState(0),
    [playing, setPlaying] = useState(false),
    [filter, setFilter] = useState(""),
    [error, setError] = useState(""),
    [tab, setTab] = useState("Fleet");
  useEffect(() => {
    let active = true;
    async function refresh() {
      try {
        const list = await api("/runs");
        if (active) setRuns(list);
        if (selected) {
          const value = await api("/runs/" + selected);
          if (active) setRun(value);
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : String(e));
      }
    }
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [selected]);
  useEffect(() => {
    setCursor(0);
    setReplay(null);
    setPlaying(false);
  }, [selected]);
  useEffect(() => {
    if (!selected || !run?.manifest) return;
    let active = true;
    api(
      `/runs/${selected}/replay?until_ns=${Math.round(cursor * 1e9)}&limit=250${inspect ? "&worker=" + inspect : ""}`,
    )
      .then((value) => {
        if (active) setReplay(value);
      })
      .catch((e) => {
        if (active) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      active = false;
    };
  }, [selected, cursor, run?.state, inspect]);
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(
      () =>
        setCursor((value) =>
          Math.min(value + 1, (run?.manifest?.horizon_ns ?? 0) / 1e9),
        ),
      1000,
    );
    return () => clearInterval(timer);
  }, [playing, run?.manifest?.horizon_ns]);
  useEffect(() => {
    if (playing && cursor >= (run?.manifest?.horizon_ns ?? 0) / 1e9)
      setPlaying(false);
  }, [playing, cursor, run?.manifest?.horizon_ns]);
  async function launch(search = false) {
    try {
      setError("");
      if (search && (targets.trim() || settings.faultCount > 0))
        throw new Error(
          "Configuration search requires scenario-relative fault targets. Remove explicit replica IDs and set the fault count to 0.",
        );
      const value = await api(
        search ? "/search" : "/runs",
        search
          ? { q, seed, suite, overrides }
          : { regular, spot, policy, scenario, rate, seed, overrides },
      );
      setSelected(value.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const horizon = (run?.manifest?.horizon_ns ?? 0) / 1e9,
    metrics = run?.metrics;
  const events = (replay?.events ?? []).filter(
    (e) =>
      !filter || JSON.stringify(e).toLowerCase().includes(filter.toLowerCase()),
  );
  return (
    <div className="app">
      <aside>
        <div className="brand">
          ◈ <b>OptiTrain</b>
          <span>FLEET LAB</span>
        </div>
        <p className="label">SIMULATIONS</p>
        <button
          className="new"
          onClick={() => {
            setSelected("");
            setRun(null);
          }}
        >
          + New simulation
        </button>
        <div className="history">
          {runs.map((r) => (
            <button
              className={selected === r.id ? "active" : ""}
              key={r.id}
              onClick={() => setSelected(r.id)}
            >
              <span>
                {r.search ? "Configuration search" : "Fleet simulation"}
              </span>
              <small>
                {r.id.slice(0, 8)} · {displayLabel(r.state)}
              </small>
            </button>
          ))}
        </div>
        <div className="foot">
          CPU analytical simulator
          <br />
          RTX PRO 6000 · Llama 3.1 8B
          <br />
          Assumed prices · synthetic traffic
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="label">DISTRIBUTED INFERENCE</p>
            <h1>Fleet resilience analysis</h1>
            <p>
              Evaluate fleet cost, request performance, and recovery under
              failure scenarios.
            </p>
          </div>
          <div>
            <span className="badge">
              {run ? displayLabel(run.state) : "Ready to configure"}
            </span>
            {run && ["queued", "running"].includes(run.state) && (
              <button
                className="cancel"
                onClick={() =>
                  api(`/runs/${selected}/cancel`, {}).catch((e) =>
                    setError(e instanceof Error ? e.message : String(e)),
                  )
                }
              >
                Cancel
              </button>
            )}
          </div>
        </header>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        <section className="config">
          <div>
            <label>
              Regular replicas
              <input
                aria-label="Regular replicas"
                type="number"
                min="0"
                max="16"
                value={regular}
                onChange={(e) => setRegular(+e.target.value)}
              />
            </label>
            <small>
              ${settings.regularPrice.toFixed(2)} / instance hour · assumed
            </small>
          </div>
          <div>
            <label>
              Spot replicas
              <input
                aria-label="Spot replicas"
                type="number"
                min="0"
                max="16"
                value={spot}
                onChange={(e) => setSpot(+e.target.value)}
              />
            </label>
            <small>
              ${settings.spotPrice.toFixed(2)} / instance hour · assumed
            </small>
          </div>
          <label>
            Routing
            <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
              <option value="round_robin">Round robin</option>
              <option value="least_work">Least observed work</option>
            </select>
          </label>
          <label>
            Scenario
            <select
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
            >
              {[
                "healthy",
                "single_spot",
                "half",
                "correlated_spot",
                "regular_crash",
                "staggered",
                "all_down",
              ].map((s) => (
                <option key={s} value={s}>
                  {scenarioLabels[s]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Requests per second
            <input
              aria-label="Requests per second"
              type="number"
              min="1"
              value={rate}
              onChange={(e) => setRate(+e.target.value)}
            />
          </label>
          <label>
            Random seed
            <input
              aria-label="Random seed"
              type="number"
              value={seed}
              onChange={(e) => setSeed(+e.target.value)}
            />
          </label>
          <button
            className="primary"
            disabled={regular + spot < 1 || regular + spot > 16}
            onClick={() => launch()}
          >
            Run simulation
          </button>
        </section>
        <details className="editor">
          <summary>
            Failure, recovery, pricing, and service-level settings
          </summary>
          <div className="control-grid">
            {(
              [
                ["faultTime", "Fault time, seconds (−1 = midpoint)"],
                ["faultCount", "Failed replica count (0 = preset)"],
                [
                  "availability",
                  "Replacement availability delay, seconds (−1 = preset)",
                ],
                ["init", "Initialization, seconds"],
                ["load", "Model loading, seconds"],
                ["discovery", "Discovery delay, seconds"],
                ["delivery", "Delivery delay, seconds"],
                ["regularPrice", "Regular price, USD per instance-hour"],
                ["spotPrice", "Spot price, USD per instance-hour"],
                ["completion", "Minimum request completion, %"],
                ["compliance", "Minimum SLO compliance across all requests, %"],
                ["ttft", "TTFT limit, seconds"],
                ["itl", "Per-token latency limit, milliseconds"],
              ] as [keyof typeof settings, string][]
            ).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  aria-label={label}
                  type="number"
                  step={
                    ["regularPrice", "spotPrice", "ttft", "delivery"].includes(
                      key,
                    )
                      ? ".01"
                      : "1"
                  }
                  value={settings[key]}
                  onChange={(e) => setSetting(key, +e.target.value)}
                />
              </label>
            ))}
            <label>
              Explicit failure targets: replica IDs, comma-separated
              <input
                aria-label="Explicit failure targets"
                placeholder="Use scenario preset"
                value={targets}
                onChange={(e) => setTargets(e.target.value)}
              />
            </label>
          </div>
          <p>
            Failures affect every replica on the selected physical nodes.
            Explicit replica IDs override the count and scenario preset for
            fixed-fleet simulations. Configuration search uses scenario-relative
            targets; remove explicit IDs and set the failed replica count to 0.
            Replacement availability defaults to 60 seconds, or 120 seconds for
            correlated spot failures. An entered delay applies to every selected
            scenario.
          </p>
        </details>
        <div className="metrics">
          {[
            ["Request completion", pct(metrics?.completion_rate)],
            ["SLO compliance · all requests", pct(metrics?.compliance_rate)],
            [
              "TTFT p95 · completed requests",
              metrics?.conditional_ttft_p95_ns == null
                ? "—"
                : seconds(metrics.conditional_ttft_p95_ns) + " s",
            ],
            [
              "Modeled cost",
              metrics?.total_usd == null
                ? "—"
                : "$" + metrics.total_usd.toFixed(4),
            ],
          ].map(([label, value]) => (
            <article key={label}>
              <p>{label}</p>
              <strong>{value}</strong>
            </article>
          ))}
        </div>
        {metrics && (
          <section className="charts">
            <Histogram
              title="Completed requests · TTFT"
              data={metrics.distributions?.completed_ttft}
            />
            <Histogram
              title="Completed streams · inter-token latency"
              data={metrics.distributions?.completed_itl}
            />
          </section>
        )}
        <nav>
          {["Fleet", "Requests", "Events", "Search", "Assumptions"].map((t) => (
            <button
              className={t === tab ? "chosen" : ""}
              key={t}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
        {["Fleet", "Requests", "Events"].includes(tab) && (
          <section className="playback">
            <button onClick={() => setPlaying(!playing)} disabled={!horizon}>
              {playing ? "Pause" : "Play"}
            </button>
            <input
              aria-label="Replay time"
              type="range"
              min="0"
              max={horizon || 1}
              step=".001"
              value={cursor}
              onChange={(e) => setCursor(+e.target.value)}
            />
            <code>
              {cursor.toFixed(3)} / {horizon.toFixed(3)} s
            </code>
            <label>
              Inspect replica
              <select
                aria-label="Inspect replay replica"
                value={inspect}
                onChange={(e) => setInspect(e.target.value)}
              >
                <option value="">All replicas</option>
                {Array.from(
                  {
                    length:
                      (run?.manifest?.regular ?? regular) +
                      (run?.manifest?.spot ?? spot),
                  },
                  (_, i) => (
                    <option key={i} value={i}>
                      {i}
                    </option>
                  ),
                )}
              </select>
            </label>
            <small>
              Read-only replay · configuration changes create a new simulation
            </small>
          </section>
        )}
        {tab === "Fleet" && (
          <>
            <section className="fleet">
              {Object.entries(replay?.workers ?? {}).map(([id, w]) => (
                <article
                  className={"worker " + (w.state ?? "").toLowerCase()}
                  key={id}
                >
                  <div>
                    <b>Replica {id.padStart(2, "0")}</b>
                    <span>
                      {+id < (run?.manifest?.regular ?? regular)
                        ? "Regular"
                        : "Spot"}
                    </span>
                  </div>
                  <strong>{w.state}</strong>
                  <p>
                    Router status:{" "}
                    {w.observed_ready ? "available" : "unavailable"}
                  </p>
                  <div className="bar">
                    <i
                      style={{
                        width:
                          Math.min(
                            100,
                            (100 * (w.stats?.kv_bytes ?? 0)) /
                              (w.stats?.kv_capacity_bytes || 1),
                          ) + "%",
                      }}
                    />
                  </div>
                  <small>
                    KV {((w.stats?.kv_bytes ?? 0) / 1024 ** 3).toFixed(2)} GiB ·
                    incarnation {w.incarnation}
                  </small>
                  <small>
                    Active {w.stats?.active ?? 0} · waiting{" "}
                    {w.stats?.waiting ?? 0}
                  </small>
                </article>
              ))}
            </section>
            {!replay && (
              <div className="empty">
                Run a simulation to view the fleet timeline.
              </div>
            )}
            <p className="note">
              Replica state reflects the physical node. Router observations
              update after the configured discovery delay. The KV cache resets
              after a crash. Modeled KV capacity excludes activation memory and
              CUDA reserves.
            </p>
          </>
        )}
        {tab === "Requests" && (
          <div className="table">
            <table>
              <thead>
                <tr>
                  <th>Request</th>
                  <th>State</th>
                  <th>Replica</th>
                  <th>Tokens delivered</th>
                  <th>Changed at</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(replay?.requests ?? {}).map(([id, r]) => (
                  <tr key={id}>
                    <td>#{id}</td>
                    <td>{r.state ? displayLabel(r.state) : "—"}</td>
                    <td>{r.worker ?? "—"}</td>
                    <td>{r.delivered}</td>
                    <td>{seconds(r.time_ns)} s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {tab === "Events" && (
          <section>
            <input
              className="filter"
              placeholder="Filter events by type, request, or replica"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <div className="events">
              {events.map((e) => (
                <div key={e.seq}>
                  <code>{seconds(e.time_ns)} s</code>
                  <b>{displayLabel(e.event)}</b>
                  <span>
                    {e.worker == null ? "" : `replica ${e.worker}`}{" "}
                    {e.id == null ? "" : `request ${e.id}`} {e.state ?? ""}
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}
        {tab === "Search" && (
          <section className="search">
            <h2>Configuration search</h2>
            <p>
              Compare 152 fleet configurations across two routing policies and{" "}
              {suite.length} selected scenarios using identical request
              arrivals. Select the configuration with the lowest maximum
              scenario cost that meets every selected service-level requirement.
            </p>
            <div className="suite">
              <p>Required scenarios</p>
              {[
                "healthy",
                "single_spot",
                "half",
                "correlated_spot",
                "regular_crash",
                "staggered",
                "all_down",
                "half_high",
              ].map((name) => (
                <label key={name}>
                  <input
                    type="checkbox"
                    checked={suite.includes(name)}
                    onChange={(e) =>
                      setSuite((values) =>
                        e.target.checked
                          ? [...values, name]
                          : values.filter((v) => v !== name),
                      )
                    }
                  />
                  {scenarioLabels[name]}
                </label>
              ))}
            </div>
            <label>
              Calibrated single-replica capacity, requests per second
              <input
                type="number"
                min=".1"
                value={q}
                onChange={(e) => setQ(+e.target.value)}
              />
            </label>
            <button
              className="primary"
              disabled={!q || !suite.length}
              onClick={() => launch(true)}
            >
              Run configuration search
            </button>
            <p>
              30-minute wall limit · 3,200 runs · 20 GiB · 2 CPU jobs maximum
            </p>
            {run?.search && (
              <pre>
                {JSON.stringify(
                  {
                    "Search status": run.search.status,
                    "Complete coverage": run.search.coverage_complete,
                    "Evaluated scenarios": run.search.runs,
                    "Planned scenarios": run.search.planned_runs,
                    "Feasible configurations": run.search.feasible_pairs,
                    "Selected configuration": run.search.winner,
                    "Stop reason": run.search.stop_reason,
                  },
                  null,
                  2,
                )}
              </pre>
            )}
            {run?.progress && (
              <p>
                Evaluated {run.progress.runs} of {run.progress.planned_runs}{" "}
                scenarios
              </p>
            )}
          </section>
        )}
        {tab === "Assumptions" && (
          <section className="assumptions">
            <h2>Model assumptions</h2>
            <p>
              Assumed USD instance-hour prices, with per-second proration.
              Allocated initialization, loading, idle time and process failure
              are billed. Terminated replacement wait is excluded. Lost work is
              already part of elapsed billing.
            </p>
            <p>
              Default settings use a 5-second health and workload observation
              delay, 5-second initialization, and 30-second model loading.
              Replacement availability takes 60 seconds, or 120 seconds for
              correlated spot failures. Requests allow up to three attempts
              before the first delivered token; interrupted streams fail. Every
              request retains its original 60-second deadline.
            </p>
            <p>
              Completion ≥ 99%; compliant requests ≥ 95% of all offered
              requests; TTFT ≤ 1 second and every ITL ≤ 100 ms. Conditional
              latency metrics describe completed requests.
            </p>
            <p>
              Analytical execution uses the pinned scheduler, KV-cache
              implementation, and kernel profiles. GPU execution and AWS failure
              behavior have not been validated against a live deployment.
            </p>
            <h3>Simulation provenance</h3>
            <pre>
              {JSON.stringify(run?.manifest?.provenance ?? {}, null, 2)}
            </pre>
          </section>
        )}
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
