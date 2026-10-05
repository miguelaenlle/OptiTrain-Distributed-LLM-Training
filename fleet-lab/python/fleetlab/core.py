"""Thin JSON transport to the authored C++ metrics/replay/search core."""

import json
import subprocess

from .adapter import ROOT


def call(value):
    process = subprocess.run(
        [str(ROOT / ".cache/fleetlab-core")],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(process.stdout)
