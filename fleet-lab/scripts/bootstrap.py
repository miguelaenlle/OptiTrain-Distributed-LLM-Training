"""Fetch exact upstream revisions and apply the opt-in integration patches."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "fleet-lab"
UPSTREAM = ROOT / ".cache" / "LLMServingSim"


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True)


def main():
    lock = json.loads((LAB / "upstream-lock.json").read_text())
    if not UPSTREAM.exists():
        UPSTREAM.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/casys-kaist/LLMServingSim.git",
                str(UPSTREAM),
            ],
            check=True,
        )
        git(UPSTREAM, "fetch", "--depth", "1", "origin", lock["llmservingsim"])
        git(UPSTREAM, "checkout", lock["llmservingsim"])
    revision = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != lock["llmservingsim"]:
        raise RuntimeError(
            "Existing upstream checkout has a different revision; "
            "preserve it and choose a fresh cache"
        )
    git(UPSTREAM, "submodule", "update", "--init", "astra-sim")
    astra = UPSTREAM / "astra-sim"
    git(
        astra,
        "submodule",
        "update",
        "--init",
        "extern/helper/fmt",
        "extern/helper/spdlog",
        "extern/graph_frontend/chakra",
        "extern/memory_backend/analytical",
        "extern/network_backend/analytical",
    )
    network = astra / "extern/network_backend/analytical"
    git(network, "submodule", "update", "--init", "extern/yaml-cpp")
    for repo, name in [
        (network, "network"),
        (astra, "astra"),
        (astra / "extern/memory_backend/analytical", "memory"),
    ]:
        patch = LAB / "patches" / f"{name}.patch"
        applied = subprocess.run(
            ["git", "-C", str(repo), "apply", "--reverse", "--check", str(patch)],
            capture_output=True,
        )
        if applied.returncode:
            git(repo, "apply", "--check", str(patch))
            git(repo, "apply", str(patch))
    print("Pinned upstream and analytical dependencies ready; integration patches applied.")


if __name__ == "__main__":
    main()
