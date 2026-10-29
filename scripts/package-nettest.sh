#!/usr/bin/env bash
# No provisioning. Produces a Linux artifact for hosts named in a test config.
set -euo pipefail
platform="${1:-linux/amd64}"
case "$platform" in linux/amd64|linux/arm64) ;; *) echo 'use linux/amd64 or linux/arm64' >&2; exit 2;; esac
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
tag="optitrain-nettest-package:${platform#linux/}"
docker build --platform "$platform" -f netfault/Dockerfile --target package -t "$tag" .
container="$(docker create "$tag")"
trap 'docker rm -f "$container" >/dev/null' EXIT
mkdir -p dist
docker cp "$container:/opt/nettest-linux.tar.gz" "dist/nettest-${platform/\//-}.tar.gz"
echo "dist/nettest-${platform/\//-}.tar.gz"
