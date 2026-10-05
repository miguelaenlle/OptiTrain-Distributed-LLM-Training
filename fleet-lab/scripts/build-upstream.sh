#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
upstream="$root/.cache/LLMServingSim"
cd "$upstream"
# Only converter/schema are needed for CPU simulation, not the HTA/torch trace
# analysis package or a gRPC runtime. Use the pinned source namespaces directly.
mkdir -p "$root/.cache/chakra-runtime/chakra"
ln -sfn "$upstream/astra-sim/extern/graph_frontend/chakra/src" "$root/.cache/chakra-runtime/chakra/src"
ln -sfn "$upstream/astra-sim/extern/graph_frontend/chakra/schema" "$root/.cache/chakra-runtime/chakra/schema"
bash astra-sim/build/astra_analytical/build.sh -t congestion_unaware
