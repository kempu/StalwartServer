# syntax=docker/dockerfile:1
#
# Stalwart at the commit upstream.lock pins, with patches/ applied. scripts/ci-prepare.sh
# prepares the build context (the patched upstream tree) and copies this file into it; every
# ARG below comes from upstream.lock and none has a default, so nothing here can drift.
#
# The stages are upstream's own Dockerfile, with three deliberate differences (README.md,
# "Building"): every image is pinned by digest; only CARGO_FEATURES is compiled, without
# `enterprise`; and the release profile uses thin LTO instead of fat.

ARG CHEF_IMAGE
ARG RUNTIME_IMAGE

FROM --platform=$BUILDPLATFORM ${CHEF_IMAGE} AS chef
WORKDIR /build

FROM --platform=$BUILDPLATFORM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path /recipe.json

FROM --platform=$BUILDPLATFORM chef AS builder
ARG TARGETPLATFORM
ARG CARGO_FEATURES
ARG BUILD_JOBS
RUN test -n "$CARGO_FEATURES" && test -n "$BUILD_JOBS" || { echo "CARGO_FEATURES and BUILD_JOBS come from upstream.lock" >&2; exit 1; }
RUN case "${TARGETPLATFORM}" in \
    "linux/arm64") echo "aarch64-unknown-linux-gnu" > /target.txt && echo "-C linker=aarch64-linux-gnu-gcc" > /flags.txt ;; \
    "linux/amd64") echo "x86_64-unknown-linux-gnu" > /target.txt && echo "-C linker=x86_64-linux-gnu-gcc" > /flags.txt ;; \
    *) echo "unsupported TARGETPLATFORM '${TARGETPLATFORM}'" >&2; exit 1 ;; \
    esac
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -yq --no-install-recommends build-essential libclang-19-dev \
    g++-aarch64-linux-gnu binutils-aarch64-linux-gnu \
    g++-x86-64-linux-gnu binutils-x86-64-linux-gnu
RUN rustup target add "$(cat /target.txt)"
# Thin LTO and 16 codegen units instead of upstream's fat LTO with one, which needs several
# times the memory and time. The thin-LTO link of the server still needs about 4 GB on its own,
# so the builder needs 6 GB (README.md, "Building"). BUILD_JOBS is how many rustc processes run
# at once.
ENV CARGO_PROFILE_RELEASE_LTO=thin \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
COPY --from=planner /recipe.json /recipe.json
# No --locked on the cook: cargo-chef replaces this workspace's own crates with placeholders,
# so the lock file cannot match at this stage. The real build below is --locked, against the
# real tree, and that is the build that produces the binary.
RUN RUSTFLAGS="$(cat /flags.txt)" cargo chef cook --jobs "$BUILD_JOBS" --target "$(cat /target.txt)" --release --no-default-features --features "$CARGO_FEATURES" --recipe-path /recipe.json
COPY . .
RUN RUSTFLAGS="$(cat /flags.txt)" cargo build --locked --jobs "$BUILD_JOBS" --target "$(cat /target.txt)" --release -p stalwart --no-default-features --features "$CARGO_FEATURES"
RUN mv "/build/target/$(cat /target.txt)/release" "/output"

FROM ${RUNTIME_IMAGE}
ARG SOURCE_URL
ARG SOURCE_REVISION
ARG RELEASE
ARG SEARCH_INDEX_FORMAT
RUN test -n "$SEARCH_INDEX_FORMAT" || { echo "SEARCH_INDEX_FORMAT comes from upstream.lock" >&2; exit 1; }
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -yq --no-install-recommends ca-certificates curl libcap2-bin && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r -g 2000 stalwart && \
    useradd -r -u 2000 -g 2000 -s /usr/sbin/nologin -M stalwart && \
    mkdir -p /etc/stalwart /var/lib/stalwart && \
    chown stalwart:stalwart /etc/stalwart /var/lib/stalwart
COPY --from=builder --chmod=0755 /output/stalwart /usr/local/bin/stalwart
RUN setcap 'cap_net_bind_service=+ep' /usr/local/bin/stalwart
# Where this binary's source is, inside the image itself as well as on it (AGPL-3.0 §6, §13).
RUN mkdir -p /usr/share/doc/stalwart && \
    printf 'This is %s, built from %s at %s.\nIts complete source: that repository at that revision, which pins the upstream\ncommit and carries every patch applied to it. License: AGPL-3.0-only.\n' \
    "$RELEASE" "$SOURCE_URL" "$SOURCE_REVISION" > /usr/share/doc/stalwart/SOURCE
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${RELEASE}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      io.github.kempu.stalwart-server.search-index-format="${SEARCH_INDEX_FORMAT}"
USER stalwart
WORKDIR /var/lib/stalwart
VOLUME ["/etc/stalwart", "/var/lib/stalwart"]
EXPOSE	443 25 110 587 465 143 993 995 4190 8080
ENV STALWART_HEALTHCHECK_URL=https://127.0.0.1:443/healthz/live
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsSk -H "X-Forwarded-For: 127.0.0.1" "$STALWART_HEALTHCHECK_URL" || curl -fsS -H "X-Forwarded-For: 127.0.0.1" http://127.0.0.1:8080/healthz/live || exit 1
ENTRYPOINT ["/usr/local/bin/stalwart"]
CMD ["--config", "/etc/stalwart/config.json"]
