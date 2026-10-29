#!/usr/bin/env bash
set -euo pipefail
ctest --test-dir .fleet/netfault-build --output-on-failure
go -C router-go test -race -timeout 60s ./...
go -C loadgen test -race -timeout 60s ./...
python3 -m pytest tests/nettest -q
python3 -m orchestrator fleet net-test --config experiments/network/quick.json
