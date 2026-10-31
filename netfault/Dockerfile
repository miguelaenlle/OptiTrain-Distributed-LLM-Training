# Build/test only this repository. CPU-only; no AWS or model download.
FROM golang:1.27-bookworm AS golang
FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ca-certificates python3 python3-pytest python3-yaml openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=golang /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" PYTHONPATH=/opt/optitrain/src GOPATH=/go
WORKDIR /opt/optitrain
COPY . .
ARG SANITIZERS=OFF
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build --mount=type=cache,target=/opt/netfault-deps \
    cmake -S netfault -B .fleet/netfault-build -DNETFAULT_SANITIZERS=${SANITIZERS} \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo -DFETCHCONTENT_BASE_DIR=/opt/netfault-deps \
    && cmake --build .fleet/netfault-build --parallel 4 \
    && mkdir -p .fleet/nettest-bin \
    && cp .fleet/netfault-build/optitrain-netfault .fleet/nettest-bin/ \
    && cd router-go && CGO_ENABLED=0 go build -trimpath -o ../.fleet/nettest-bin/router . \
    && cd ../loadgen && CGO_ENABLED=0 go build -trimpath -o ../.fleet/nettest-bin/loadgen . \
    && cp -a /go/pkg/mod /opt/optitrain/.fleet/go-mod
FROM build AS test
ENV GOMODCACHE=/opt/optitrain/.fleet/go-mod
ENV NETFAULT_BINARY=/opt/optitrain/.fleet/nettest-bin/optitrain-netfault
CMD ["bash", "scripts/test-nettest-linux.sh"]
FROM build AS package
RUN mkdir -p /bundle/bin && cp .fleet/nettest-bin/* /bundle/bin/ \
    && cp src/orchestrator/nettest/agent.py src/orchestrator/nettest/runtime.py /bundle/ \
    && cp -r .fleet/netfault-build/licenses /bundle/ \
    && cp /usr/local/go/LICENSE /bundle/licenses/GO-LICENSE \
    && find .fleet/go-mod -type f \( -iname 'LICENSE*' -o -iname 'NOTICE*' -o -iname 'COPYING*' \) \
       -exec cp --parents '{}' /bundle/licenses/ \; \
    && tar -C /bundle -czf /opt/nettest-linux.tar.gz .
FROM ubuntu:24.04 AS ssh-test
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd /root/.ssh && passwd -d root
COPY --from=package /opt/nettest-linux.tar.gz /opt/nettest-linux.tar.gz
# A test fixture supplies authorized_keys and host keys at runtime.
CMD ["/usr/sbin/sshd", "-D", "-e"]
