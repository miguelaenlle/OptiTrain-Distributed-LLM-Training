#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
export FLEETLAB_SOURCE="$root/fleet-lab/cpp"
cmake --compile-no-warning-as-error -S "$root/.cache/LLMServingSim/astra-sim/build/astra_analytical" -B "$root/.cache/asan-build" -DBUILDTARGET=congestion_unaware -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS="-Wno-error=reorder -Wno-error=deprecated-declarations"
cmake --build "$root/.cache/asan-build" --target AstraSim_Analytical_Congestion_Unaware -j 4
export PYTHONPATH="$root/fleet-lab/python"
export FLEETLAB_BINARY="$root/.cache/asan-build/bin/AstraSim_Analytical_Congestion_Unaware"
# Upstream Sys/Logger ownership is process-lifetime and is not leak-clean.
# Address/undefined checks remain enabled for real cancellation/recovery tests.
export ASAN_OPTIONS=detect_leaks=0
pytest -q "$root/fleet-lab/tests/test_engine.py"
network="$root/.cache/LLMServingSim/astra-sim/extern/network_backend/analytical"
g++ -std=c++17 -g -fsanitize=address,undefined -I "$network/include" -I "$network/include/astra-network-analytical" \
    "$root/fleet-lab/cpp/test_clock.cc" "$network/common/event-queue/Event.cpp" \
    "$network/common/event-queue/EventList.cpp" "$network/common/event-queue/EventQueue.cpp" -o "$root/.cache/test-clock-asan"
ASAN_OPTIONS=detect_leaks=1 "$root/.cache/test-clock-asan"
