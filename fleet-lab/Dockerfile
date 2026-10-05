FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ cmake make git ca-certificates libprotobuf-dev protobuf-compiler \
    python3 python3-venv python3-dev libboost-program-options-dev clang clang-format \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/fleetlab
ENV PATH="/opt/fleetlab/bin:${PATH}"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /workspace
