#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
export FLEETLAB_SOURCE="$root/fleet-lab/cpp"
bash "$root/fleet-lab/scripts/build-upstream.sh"

g++ -std=c++17 -O2 -Wall -Wextra -I "$root/.cache/LLMServingSim/astra-sim/extern/helper" "$root/fleet-lab/cpp/core.cc" -o "$root/.cache/fleetlab-core"
