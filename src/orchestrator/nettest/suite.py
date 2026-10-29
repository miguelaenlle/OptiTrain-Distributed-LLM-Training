"""Run the fixed preset matrix; regenerate its report from checksummed evidence."""

from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from . import config, runner
from .report import delta, evaluate

RECIPE = config.ROOT / "experiments/network/report.json"
SCENARIOS = ["baseline", *sorted(set(config.PRESETS) - {"baseline"})]


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def source():
    revision = os.environ.get("OPTITRAIN_SOURCE_REVISION", "unknown")
    result = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=config.ROOT, capture_output=True, text=True
        )
        if shutil.which("git")
        else None
    )
    if result and result.returncode == 0:
        revision = result.stdout.strip()
    return {
        "revision": revision,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "files_sha256": {
            str(path.relative_to(config.ROOT)): digest(path)
            for path in sorted((config.ROOT / "src/orchestrator/nettest").glob("*.py"))
        },
    }


def verified(root, relative, expected):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"evidence path escapes suite: {relative}")
    if not path.is_file() or digest(path) != expected:
        raise ValueError(f"missing or changed evidence: {relative}")
    return path


def inspect_case(root, case):
    if case["state"] != "complete":
        return None
    for relative, checksum in case["files_sha256"].items():
        verified(root, relative, checksum)
    folder = root / case["directory"]
    stored = json.loads((folder / "report.json").read_text())
    cfg = json.loads((folder / "config.json").read_text())
    requests = (
        runner.jsonl((folder / "requests.jsonl").read_bytes())
        if (folder / "requests.jsonl").exists()
        else []
    )
    identities = {row["request_id"] for row in requests}
    attempts = (
        runner.jsonl((folder / "attempts.jsonl").read_bytes())
        if (folder / "attempts.jsonl").exists()
        else []
    )
    attempts = [row for row in attempts if row["request_id"] in identities]
    computed = evaluate(cfg, requests, stored["windows"], attempts, stored["evidence_errors"])
    computed["profile_kind"] = stored["profile_kind"]
    if computed != stored:
        raise ValueError(f"report disagrees with raw request/attempt evidence: {case['scenario']}")
    if {"PASS": 0, "FAIL": 1, "INVALID": 2}[computed["verdict"]] != case["exit_code"]:
        raise ValueError(f"exit code disagrees with verdict: {case['scenario']}")
    return computed


def number(value):
    return "N/A" if value is None else f"{value:.2f}"


def render(index, reports):
    base = index["experiment"]
    lines = [
        "# Network failure scenario report",
        "",
        "Generated from recorded measurements. FAIL means the unchanged acceptance "
        "contract was missed; "
        "INVALID means setup/execution/evidence was invalid. Neither is converted to PASS.",
        "",
        f"Source: `{index['source']['revision']}`. Environment: {index['source']['platform']}; "
        f"Python {index['source']['python']}. Seed: **{index['seed']}**.",
        "",
        f"Workload: **{base['workers']} workers, {base['rps']} RPS, "
        f"concurrency {base['concurrency']}, "
        f"{base['input_tokens']} input / {base['output_tokens']} output events**. "
        f"Phases: {base['phases']['healthy_s']}s healthy → {base['phases']['fault_s']}s fault → "
        f"{base['phases']['recovery_s']}s recovery. "
        "Scenarios execute sequentially with fresh processes.",
        "",
        f"Timing source: **{index['profile']['kind']}**. "
        f"Settings: `{json.dumps(index['profile']['settings'], sort_keys=True)}`. "
        "Model-derived schedules are estimates, not measured GPU serving. "
        "Each replay worker runs one request at a time; no continuous batching or KV-cache model.",
        "",
        f"Normal SLO: complete, TTFT ≤{base['slo']['ttft_ms']}ms, every output gap "
        f"≤{base['slo']['gap_ms']}ms, total ≤{base['slo']['total_ms']}ms. "
        "The same acceptance criteria apply to every scenario; "
        "severe intentional outages can fail them.",
        "",
        "Inputs, source files, binaries and case evidence have SHA-256 records in `suite.json`. "
        "Metrics below are recomputed from raw requests/attempts "
        "and checked against each original report.",
        "",
        "[Recorded evidence bundle](evidence.tar.gz). Extract it to inspect the case files below "
        "or regenerate this report without running traffic.",
        "",
        "## Overview",
        "",
        "Fault-phase metrics; latency percentiles only describe received output. "
        "Failures/drops remain in completion and SLO denominators. "
        "N/A means no observed output, not zero latency.",
        "",
        "| Scenario | Verdict | Completed (all offered) | Fault SLO % | "
        "Fault TTFT p95 ms | Fault gap p99 ms | Fault goodput RPS |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for case, report in zip(index["cases"], reports, strict=True):
        if report is None:
            lines.append(f"| {case['scenario']} | NOT RUN | — | — | — | — | — |")
            continue
        fault = report["phases"]["fault"]
        lines.append(
            f"| {case['scenario']} | {report['verdict']} | "
            f"{report['all_requests']['completed']}/{report['all_offered']} "
            f"| {number(fault['compliance_pct'])} | {number(fault['ttft_p95_ms'])} "
            f"| {number(fault['gap_p99_ms'])} | {number(fault['goodput_rps'])} |"
        )
    for case, report in zip(index["cases"], reports, strict=True):
        lines += ["", f"## {case['scenario']}", ""]
        if report is None:
            lines.append("NOT RUN. No finding is inferred for an unexecuted scenario.")
            continue
        cfg = case["experiment"]
        fault = json.dumps(cfg["fault"], sort_keys=True) if cfg["fault"] else "No proxy rule."
        if cfg["scenario"] == "client-disconnect":
            fault = (
                "Client cancels after five nonempty output events, for fault-phase dispatches only."
            )
        lines += [
            f"**{report['verdict']}**. Fault: `{fault}`",
            "",
            "| Metric | Healthy | Fault (change) | Recovery (change) |",
            "|---|---:|---:|---:|",
        ]
        for metric in (
            "completion_pct",
            "compliance_pct",
            "ttft_p95_ms",
            "gap_p99_ms",
            "goodput_rps",
        ):
            healthy = report["phases"]["healthy"][metric]
            values = [number(healthy)]
            for phase in ("fault", "recovery"):
                value = report["phases"][phase][metric]
                values.append(f"{number(value)} ({delta(value, healthy, metric.endswith('_pct'))})")
            lines.append(f"| {metric} | {' | '.join(values)} |")
        lines += [
            "",
            f"All offered: **{report['all_offered']}**; completed: "
            f"**{report['all_requests']['completed']}**; boundary/guard requests retained: "
            f"**{report['transition']['offered']}**.",
        ]
        recovery = report["recovery"]
        lines += [
            "",
            (
                f"Recovery stability window starts +{recovery['window_start_s']:.2f}s "
                "after removal; "
                f"confirmed +{recovery['confirmed_s']:.2f}s ({recovery['requests']} requests). "
                "This is not node restart time."
            )
            if recovery
            else "No recovery window qualified under the declared criteria.",
        ]
        failed = [check for check in report["checks"] if check["verdict"] != "PASS"]
        lines += ["", "Failed checks:" if failed else "All required checks passed."]
        if failed:
            lines.append("")
            for check in failed:
                operator = ">=" if check["operator"] == "ge" else "<="
                lines.append(
                    f"- {check['name']}: {number(check['actual'])}; "
                    f"required {operator} {number(check['limit'])}."
                )
        if report["evidence_errors"]:
            lines += [
                "",
                "Execution/evidence errors:",
                "",
                *[f"- {error}" for error in report["evidence_errors"]],
            ]
        lines += [
            "",
            f"Evidence in bundle: `{case['directory']}/report.txt`, "
            f"`{case['directory']}/config.json`, `{case['directory']}/requests.jsonl`.",
        ]
    lines += [
        "",
        "## Reproduction boundaries",
        "",
        "The recipe, order, seed and report calculation are fixed. Regenerating from unchanged "
        "saved evidence with this source produces identical Markdown/JSON. Fresh TCP runs will "
        "have different timing samples because of CPU scheduling, buffering and TCP chunking; "
        "a seed does not make live networking deterministic. Compare distributions and verdicts, "
        "not bitwise-equal live timings. Each scenario here is one run, not a confidence interval.",
        "",
        "The pre-fault deadline guard and control-boundary exclusions are explicit; those requests "
        "are still in all-offered totals. "
        "Inter-output events are not necessarily individual tokens. "
        "No natural failure likelihood, EC2 performance or GPU throughput is inferred.",
        "",
    ]
    return "\n".join(lines)


def regenerate(root):
    root = Path(root).resolve()
    index = json.loads((root / "suite.json").read_text())
    if index.get("version") != 1:
        raise ValueError("unsupported suite evidence version")
    for relative, checksum in index["inputs_sha256"].items():
        verified(root, relative, checksum)
    reports = [inspect_case(root, case) for case in index["cases"]]
    text = render(index, reports)
    result = {
        "version": 1,
        "cases": [
            {"scenario": case["scenario"], "report": report}
            for case, report in zip(index["cases"], reports, strict=True)
        ],
    }
    save(root / "summary.json", result)
    (root / "report.md").write_text(text)
    return (
        2
        if any(report is None or report["verdict"] == "INVALID" for report in reports)
        else int(any(report["verdict"] == "FAIL" for report in reports))
    )


def bundle(root):
    paths = {"suite.json", "report.md", "summary.json"}
    index = json.loads((root / "suite.json").read_text())
    paths.update(index["inputs_sha256"])
    for case in index["cases"]:
        paths.update(case["files_sha256"])
    # Stable member order, tar metadata and gzip header; no executables/weights.
    with (
        (root / "evidence.tar.gz").open("wb") as file,
        gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for relative in sorted(paths):
            data = (root / relative).read_bytes()
            entry = tarfile.TarInfo(relative)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))


def execute(base, output, scenarios, seed=42, skip_build=False):
    if base["execution"] != "local" or base["backend"] != "replay" or base["direct"]:
        raise ValueError("preset suite requires local replay through both proxies")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs/bin").mkdir(parents=True)
    (output / "logs").mkdir()
    binaries = (
        runner.build()
        if not skip_build
        else {
            name: str(
                Path(
                    base["artifacts"].get(name, config.ROOT / ".fleet/nettest-bin" / name)
                ).resolve()
            )
            for name in ("optitrain-netfault", "router", "loadgen")
        }
    )
    for name, binary in binaries.items():
        shutil.copy2(binary, output / "inputs/bin" / name)
    profile = Path(base["profile"])
    shutil.copy2(
        profile if profile.is_absolute() else config.ROOT / profile, output / "inputs/profile.json"
    )
    save(output / "inputs/experiment.json", base)
    profile_data = json.loads((output / "inputs/profile.json").read_text())
    index = {
        "version": 1,
        "seed": seed,
        "source": source(),
        "profile": {
            "kind": profile_data["kind"],
            "settings": {
                key: value
                for key, value in profile_data.get("metadata", {}).items()
                if key
                in ("model", "system", "backend", "backend_version", "batch_size", "tp", "pp")
            },
        },
        "experiment": base,
        "inputs_sha256": {
            name: digest(output / name)
            for name in ("inputs/profile.json", "inputs/experiment.json")
        },
        "binaries_sha256": {name: digest(output / "inputs/bin" / name) for name in binaries},
        "cases": [],
    }
    for position, scenario in enumerate(scenarios, 1):
        cfg = copy.deepcopy(base)
        cfg.update(
            scenario=scenario,
            fault=copy.deepcopy(config.PRESETS[scenario]),
            profile=str(output / "inputs/profile.json"),
            output=str(output / f"{position:02d}-{scenario}"),
            artifacts={name: str(output / "inputs/bin" / name) for name in binaries},
        )
        if cfg["fault"]:
            cfg["fault"]["seed"] = seed
        config.validate(cfg)
        index["cases"].append(
            {
                "scenario": scenario,
                "directory": f"{position:02d}-{scenario}",
                "experiment": cfg,
                "state": "pending",
                "files_sha256": {},
            }
        )
    save(output / "suite.json", index)
    regenerate(output)
    interrupted = False
    try:
        for position, case in enumerate(index["cases"], 1):
            print(
                f"[{position}/{len(scenarios)}] {case['scenario']}: "
                f"{sum(base['phases'].values()):g}s; "
                f"log: {output / 'logs' / (case['directory'] + '.log')}",
                flush=True,
            )
            log_path = output / "logs" / (case["directory"] + ".log")
            with (
                log_path.open("w") as log,
                contextlib.redirect_stdout(log),
                contextlib.redirect_stderr(log),
            ):
                code = runner.run(case["experiment"])
            case.update(state="complete", exit_code=code)
            files = [path for path in (output / case["directory"]).iterdir() if path.is_file()]
            case["files_sha256"] = {
                str(path.relative_to(output)): digest(path) for path in sorted([*files, log_path])
            }
            save(output / "suite.json", index)
            regenerate(output)
            report = json.loads((output / case["directory"] / "report.json").read_text())
            print(
                f"  {report['verdict']}; completed "
                f"{report['all_requests']['completed']}/{report['all_offered']}",
                flush=True,
            )
            if any(error.startswith("KeyboardInterrupt:") for error in report["evidence_errors"]):
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        code = regenerate(output)
        bundle(output)
    print(f"Report: {output / 'report.md'}", flush=True)
    return 130 if interrupted else code


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(RECIPE), help="fixed workload/acceptance recipe")
    parser.add_argument("--output", default=".fleet/network-report", help="new output directory")
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--skip-build", action="store_true", help="use existing binaries, then freeze copies"
    )
    parser.add_argument(
        "--report-from", help="verify/recompute saved evidence without starting traffic"
    )
    parsed = parser.parse_args(args)
    try:
        if parsed.report_from:
            code = regenerate(parsed.report_from)
            bundle(Path(parsed.report_from).resolve())
            print(f"Regenerated {Path(parsed.report_from).resolve() / 'report.md'}")
            return code
        if not 0 <= parsed.seed < 2**63 or len(set(parsed.scenarios)) != len(parsed.scenarios):
            raise ValueError("seed must be in 0..2^63-1 and scenarios must be distinct")
        return execute(
            config.load(parsed.config),
            parsed.output,
            parsed.scenarios,
            parsed.seed,
            parsed.skip_build,
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"net-test suite: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
