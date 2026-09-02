# syntax=docker/dockerfile:1
#
# Multi-stage build for the mobkit library host.
# ⚠ glibc must match: pin BOTH stages to trixie. A floating rust:1-slim drifts and you get
#   `GLIBC_2.39 not found` at runtime.

FROM rust:1-slim-trixie AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config \
        libssl-dev \
        cmake \
        protobuf-compiler \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Bound parallelism so the big meerkat crates don't OOM a small host during codegen.
ENV CARGO_BUILD_JOBS=4

WORKDIR /build
COPY Cargo.toml ./
COPY src ./src

# BuildKit cache mounts keep rebuilds incremental (first compile is huge — ~591 crates).
# Because target/ is a CACHE MOUNT (not baked into the image), cp the binary to a normal
# path INSIDE the same RUN before the mount goes away.
# ⚠ stale-binary trap: the target cache can hand back an old binary when only the base image
#   changed (cargo's fingerprint ignores glibc). On a mystery glibc mismatch after a green
#   build, bump the cache-mount id= or `docker builder prune --filter type=exec.cachemount`.
RUN --mount=type=cache,id=mobkit-cargo-registry-v1,target=/usr/local/cargo/registry \
    --mount=type=cache,id=mobkit-target-v1,target=/build/target \
    cargo build --release \
    && cp target/release/mobkit-host /usr/local/bin/mobkit-host

FROM debian:trixie-slim

# ca-certificates: REQUIRED for the outbound TLS to api.openai.com (why we can drop the old
# CA-bearing proxy sidecar). mkdir the persistent-state dir (compose overlays it with tmpfs).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /srv/mobkit/state

COPY --from=builder /usr/local/bin/mobkit-host /usr/local/bin/mobkit-host

# config.toml / mob.toml are bind-mounted at /app by compose.
EXPOSE 8090
CMD ["mobkit-host"]
