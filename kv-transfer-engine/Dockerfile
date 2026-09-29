FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build libprotobuf-dev protobuf-compiler \
    libgtest-dev pybind11-dev python3-dev python3-pytest iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
