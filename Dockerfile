# syntax=docker/dockerfile:1.7
# check=skip=FromPlatformFlagConstDisallowed

# =============================================================================
# RISC-V (riscv64) Dockerfile for Home Assistant Core
#
# Build strategy:
#   - Python deps: compiled natively on riscv64 into a venv (pre-built wheels
#                  from PyPI and the RISE riscv64 wheel index where they exist)
#   - go2rtc     : cross-compiled on the build host (pure Go, no QEMU)
#   - FFmpeg 8   : cross-compiled on the build host (PyAV 19, which Home
#                  Assistant's `stream` needs, requires FFmpeg 8; Debian trixie
#                  has 7.1)
#   - s6-overlay, Home Assistant sources and rootfs: downloaded on the build
#                  host (no QEMU); the upstream rootfs/ is used unmodified
#   - Runtime    : python:3.14-slim-trixie (riscv64) + s6-overlay, same /init
#                  entrypoint and /config volume as the official image
#
# Upstream's own image cannot be used: it is built FROM
# ghcr.io/home-assistant/base-python (Alpine/musl, amd64+arm64 only) and
# installs only musllinux wheels from wheels.home-assistant.io.
# =============================================================================

# Home Assistant release (git tag). Keep GO2RTC_VERSION in sync with the
# `COPY --from=ghcr.io/alexxit/go2rtc:<version>` line of upstream's Dockerfile
# at that tag; CI reads it from there.
ARG HA_VERSION=2026.10.4
ARG GO2RTC_VERSION=1.9.14
ARG FFMPEG_VERSION=8.1.3
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG BASHIO_VERSION=0.19.0

# =============================================================================
# Stage 0 — Upstream sources + s6-overlay + bashio (build host, no QEMU)
# =============================================================================
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS source
ARG HA_VERSION
ARG S6_OVERLAY_VERSION
ARG BASHIO_VERSION

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Only what upstream's Dockerfile copies into the image: no tests, no scripts.
WORKDIR /usr/src
RUN mkdir /tmp/ha && \
    curl -fsSL --retry 5 "https://github.com/home-assistant/core/archive/refs/tags/${HA_VERSION}.tar.gz" \
        | tar xz -C /tmp/ha --strip-components=1 && \
    mkdir homeassistant && \
    cd /tmp/ha && \
    cp -r LICENSE* README* homeassistant pyproject.toml requirements.txt requirements_all.txt /usr/src/homeassistant/ && \
    cp -r rootfs /rootfs && \
    rm -rf /tmp/ha

# s6-overlay: same assets and versions as home-assistant/docker-base, but the
# riscv64 tarball (s6-overlay publishes one) and with checksum verification.
RUN set -eu; mkdir -p /s6; cd /tmp; \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"; \
    for f in s6-overlay-noarch s6-overlay-riscv64 s6-overlay-symlinks-arch s6-overlay-symlinks-noarch; do \
        curl -fsSL --retry 5 -o "$f.tar.xz" "$base/$f.tar.xz"; \
        curl -fsSL --retry 5 -o "$f.tar.xz.sha256" "$base/$f.tar.xz.sha256"; \
        sha256sum -c "$f.tar.xz.sha256"; \
        tar -C /s6 -Jxpf "$f.tar.xz"; \
    done; \
    rm -f /tmp/s6-overlay-*

RUN mkdir -p /bashio/usr/lib /bashio/usr/bin && \
    curl -fsSL --retry 5 "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
        | tar xz -C /tmp && \
    mv /tmp/bashio-*/lib /bashio/usr/lib/bashio && \
    ln -s /usr/lib/bashio/bashio /bashio/usr/bin/bashio

# =============================================================================
# Stage 1 — go2rtc, cross-compiled (build host, no QEMU)
#
# Upstream copies the binary out of ghcr.io/alexxit/go2rtc, which has no
# riscv64 manifest, and go2rtc publishes no riscv64 release asset. It is pure
# Go (CGO off), so this is a plain cross-compile of the tagged source.
# =============================================================================
FROM --platform=$BUILDPLATFORM golang:1.25-trixie AS go2rtc_builder
ARG GO2RTC_VERSION
ENV GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux GOARCH=riscv64
RUN git clone --depth 1 -b "v${GO2RTC_VERSION}" https://github.com/AlexxIT/go2rtc /src
WORKDIR /src
RUN --mount=type=cache,target=/root/.cache/go-build \
    go build -ldflags "-s -w" -trimpath -o /out/go2rtc

# =============================================================================
# Stage 2 — FFmpeg 8, cross-compiled (build host, no QEMU)
#
# PyAV 19 (pinned by Home Assistant, used by `stream`, which default_config
# loads) uses FFmpeg 8 APIs; Debian trixie ships 7.1 and the official image
# gets 8.x from Alpine 3.24. Debian's riscv64 -dev packages are installed
# through multiarch next to gcc-riscv64-linux-gnu, like the other riscv64
# cross builds, and FFmpeg's own build system does the rest.
# The result is installed to /usr/local in both the Python builder (headers and
# pkg-config files for PyAV) and the runtime image (libav* and the ffmpeg CLI).
# =============================================================================
FROM --platform=$BUILDPLATFORM debian:trixie AS ffmpeg_builder
ARG FFMPEG_VERSION
RUN dpkg --add-architecture riscv64 && \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git make pkg-config gcc libc6-dev \
        gcc-riscv64-linux-gnu libc6-dev-riscv64-cross \
        libssl-dev:riscv64 zlib1g-dev:riscv64 libx264-dev:riscv64 libvpx-dev:riscv64 \
        libopus-dev:riscv64 libmp3lame-dev:riscv64 libvorbis-dev:riscv64 \
        libwebp-dev:riscv64 libdav1d-dev:riscv64 && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 -b "n${FFMPEG_VERSION}" https://github.com/FFmpeg/FFmpeg /src
WORKDIR /src
ENV PKG_CONFIG_LIBDIR=/usr/lib/riscv64-linux-gnu/pkgconfig:/usr/share/pkgconfig
RUN ./configure \
        --prefix=/usr/local \
        --enable-cross-compile --arch=riscv64 --target-os=linux \
        --cross-prefix=riscv64-linux-gnu- --pkg-config=pkg-config \
        --extra-cflags=-I/usr/include/riscv64-linux-gnu --extra-ldflags=-L/usr/lib/riscv64-linux-gnu \
        --enable-shared --disable-static --enable-pic \
        --disable-doc --disable-ffplay \
        --enable-gpl --enable-version3 --enable-openssl \
        --enable-libx264 --enable-libvpx --enable-libopus --enable-libmp3lame \
        --enable-libvorbis --enable-libwebp --enable-libdav1d && \
    make -j"$(nproc)" && \
    make install DESTDIR=/opt/ffmpeg

# =============================================================================
# Stage 3 — PicoTTS (riscv64), same pinned commit as home-assistant/docker
# =============================================================================
FROM ${PYTHON_IMAGE} AS picotts_builder
ARG PICOTTS_HASH=e3ba46009ee868911fa0b53db672a55f9cc13b1c
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        autoconf automake build-essential ca-certificates git libpopt-dev libtool && \
    rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/naggety/picotts.git /tmp/pico && \
    cd /tmp/pico/pico && \
    git reset --hard "${PICOTTS_HASH}" && \
    ./autogen.sh && \
    ./configure --disable-static --prefix=/usr/local && \
    make -j"$(nproc)" && \
    make DESTDIR=/opt/picotts install

# =============================================================================
# Stage 4 — Builder base (runs natively on riscv64): toolchain, FFmpeg 8,
# the venv with uv. Shared by the two stages below so its layers are built once.
# =============================================================================
FROM ${PYTHON_IMAGE} AS builder_base

# Parallel compile jobs. The peak RSS of this stage is set by how many
# rustc/cc processes run at once; lower it to 1-2 if the build is OOM-killed.
ARG BUILD_JOBS=4

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_BUILD_JOBS=${BUILD_JOBS} \
    MAKEFLAGS=-j${BUILD_JOBS} \
    MAX_JOBS=${BUILD_JOBS} \
    CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS} \
    UV_CONCURRENT_BUILDS=${BUILD_JOBS} \
    GRPC_PYTHON_BUILD_EXT_COMPILER_JOBS=${BUILD_JOBS} \
    CARGO_PROFILE_RELEASE_DEBUG=false

# -dev packages for everything that still gets compiled here: Pillow's image
# libraries, the C extensions of the database drivers, USB /
# udev / D-Bus / GPIO bindings. The apt hardening mirrors the other riscv64
# images: retries and one connection at a time, because a single failed .deb
# otherwise fails the stage under QEMU and throws away everything fetched.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    printf 'Acquire::Retries "10";\nAcquire::http::Pipeline-Depth "0";\nAcquire::Queue-Mode "access";\nAcquire::ForceIPv4 "true";\n' \
        > /etc/apt/apt.conf.d/99-build-robust && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential pkg-config cmake ninja-build patchelf autoconf automake libtool git curl ca-certificates \
        libssl-dev libffi-dev zlib1g-dev libbz2-dev liblzma-dev \
        libjpeg-dev libturbojpeg0-dev libtiff-dev libwebp-dev libfreetype-dev \
        liblcms2-dev libopenjp2-7-dev \
        libpq-dev libmariadb-dev \
        libudev-dev libusb-1.0-0-dev libdbus-1-dev libglib2.0-dev libgpiod-dev \
        libxml2-dev libxslt1-dev libsodium-dev

# FFmpeg 8 headers, libraries and pkg-config files for PyAV (deliberately no
# libav*-dev from Debian: PyAV must not find the 7.1 headers).
COPY --from=ffmpeg_builder /opt/ffmpeg/usr/local/ /usr/local/
RUN ldconfig

# Rust: Debian's rustc lags the toolchains that cryptography and friends need.
# riscv64gc-unknown-linux-gnu is a Tier-2 target with host tools.
ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:${PATH}
RUN curl --proto '=https' --tlsv1.2 -fsSL --retry 5 --retry-connrefused \
      https://sh.rustup.rs -o /tmp/rustup-init.sh && \
    sh /tmp/rustup-init.sh -y --no-modify-path --profile minimal \
      --default-toolchain stable && \
    rm /tmp/rustup-init.sh && \
    rustc --version && cargo --version

# PyPI covers part of riscv64; the RISE project (RISC-V Software Ecosystem,
# Linux Foundation Europe) publishes tested wheels for much of the rest:
# https://riseproject.gitlab.io/python/wheel_builder/
# "unsafe-best-match" makes uv look at both indexes for each exact pin
# instead of stopping at the first index that knows the package name. Home
# Assistant pins everything with ==, so the only thing this changes is *where*
# a wheel comes from.
ARG UV_EXTRA_INDEX_URL=https://gitlab.com/api/v4/projects/56254198/packages/pypi/simple
ENV UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL} \
    UV_INDEX_STRATEGY=unsafe-best-match \
    UV_LINK_MODE=copy

# grpcio is the one package whose upstream pin (1.83.1) has no riscv64 wheel
# anywhere while a neighbouring release has one on RISE, and compiling it is a
# multi-hour build even in a dedicated CI job. install-requirements.py swaps
# the pin for the newest prebuilt version not newer than upstream's and records
# the swap in /usr/share/ha-riscv64/substitutes.txt. It is safe to do for this
# package because no integration manifest pins grpcio (Home Assistant checks
# manifest requirements at runtime, so a swapped pin there would break the
# integration - numpy, for one, must stay exact). Set to "" to compile the
# exact upstream version instead.
ARG RISE_SUBSTITUTES="grpcio"
ENV RISE_SUBSTITUTES=${RISE_SUBSTITUTES}

# -g0 and a non-fat-LTO release profile: the default debug info and
# single-codegen-unit Rust profiles make from-source wheels far slower to build
# under emulation for no benefit here.
ENV CFLAGS="-O2 -g0" \
    CXXFLAGS="-O2 -g0" \
    CARGO_PROFILE_RELEASE_LTO=off \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
    GRPC_PYTHON_BUILD_SYSTEM_OPENSSL=1 \
    GRPC_PYTHON_BUILD_SYSTEM_ZLIB=1

# Same layout as upstream (/usr/src/homeassistant), because the editable
# install below records that path.
COPY --from=source /usr/src/homeassistant /usr/src/homeassistant
WORKDIR /usr/src/homeassistant

# A venv so the whole result can be copied to the runtime stage as one tree
# (both stages share the same base image, so the interpreter symlink and
# shebangs stay valid).
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=${VIRTUAL_ENV}/bin:${PATH}
RUN python3 -m venv ${VIRTUAL_ENV} && \
    pip install --upgrade pip setuptools wheel && \
    pip install "uv==$(awk -F'==' '/^uv==/{print $2}' requirements.txt)"

# =============================================================================
# Stage 4a — Wheel builder: compiles the wheels that have no riscv64 build
# anywhere. CI runs this target once per shard of plan-wheels.py, in parallel
# jobs, and keeps the result in a GitHub release so that later runs (and the
# next Home Assistant release, whose pins mostly stay the same) do not compile
# them again. Not part of the final image.
#   docker run --rm -v ./plan:/plan -v ./wheels:/out wheel-builder /plan/plan.json 0 /out
# =============================================================================
FROM builder_base AS wheel_builder
COPY hadeps.py build-wheels.py /opt/build/
ENTRYPOINT ["python3", "/opt/build/build-wheels.py"]

# =============================================================================
# Stage 4b — Python builder: installs the dependency set into /opt/venv.
# Wheels found in /wheelhouse (pre-built by the shards above) are used as they
# are; anything still missing is compiled here, so a build without a wheelhouse
# is slow but still correct.
# =============================================================================
FROM builder_base AS python_builder
COPY wheelhouse/ /wheelhouse/
ENV UV_FIND_LINKS=/wheelhouse

# Database drivers the official image adds on top (home-assistant/docker
# requirements.txt) — built here against Debian's libpq and MariaDB client.
COPY requirements-base.txt /tmp/requirements-base.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -r /tmp/requirements-base.txt

# Core + integrations. See install-requirements.py for the retry/skip logic.
COPY hadeps.py install-requirements.py skip-integrations.txt /opt/build/
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/opt/cargo/registry \
    python3 /opt/build/install-requirements.py \
        /usr/src/homeassistant /opt/build/skip-integrations.txt /opt/ha-riscv64

# Home Assistant itself, editable like upstream, then byte-compile once.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e /usr/src/homeassistant && \
    python3 -m compileall -q -j0 /usr/src/homeassistant/homeassistant

# =============================================================================
# Stage 5 — Final runtime image (riscv64)
# =============================================================================
FROM ${PYTHON_IMAGE}
ARG HA_VERSION

LABEL \
    io.hass.type="core" \
    org.opencontainers.image.authors="The Home Assistant Authors" \
    org.opencontainers.image.description="Unofficial Home Assistant Core build for linux/riscv64" \
    org.opencontainers.image.documentation="https://www.home-assistant.io/docs/" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.source="https://github.com/home-assistant/core" \
    org.opencontainers.image.title="Home Assistant (riscv64)" \
    org.opencontainers.image.url="https://www.home-assistant.io/" \
    org.opencontainers.image.version="${HA_VERSION}"

# Environment of upstream's base image (home-assistant/docker-base, debian) and
# of its core Dockerfile. S6_SERVICES_GRACETIME is synchronised upstream with
# homeassistant/core.py:async_stop. The extra uv index lets Home Assistant's
# on-demand `uv pip install` of an integration's requirements find riscv64
# wheels, like wheels.home-assistant.io does for the official image.
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_CMD_WAIT_FOR_SERVICES=1 \
    S6_SERVICES_READYTIME=50 \
    S6_SERVICES_GRACETIME=240000 \
    UV_NO_CACHE=true \
    UV_EXTRA_INDEX_URL=https://gitlab.com/api/v4/projects/56254198/packages/pypi/simple \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# Runtime equivalents of the apk packages in home-assistant/docker's
# Dockerfile (bluez, ffmpeg, nmap, ...), plus the shared objects the wheels
# compiled in the builder link against.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    printf 'Acquire::Retries "10";\nAcquire::http::Pipeline-Depth "0";\nAcquire::Queue-Mode "access";\nAcquire::ForceIPv4 "true";\n' \
        > /etc/apt/apt.conf.d/99-build-robust && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        bash ca-certificates curl git grep jq tzdata xz-utils \
        bluez libbluetooth3 libconfuse2 libudev1 libusb-1.0-0 hwdata \
        iperf3 libftdi1-2 libgpiod3 libpulse0 libasound2-plugins libturbojpeg0 libzbar0t64 \
        libmariadb3 libpq5 libssl3t64 libffi8 zlib1g \
        libjpeg62-turbo libtiff6 libwebp7 libwebpmux3 libfreetype6 liblcms2-2 libopenjp2-7 libxml2 libxslt1.1 libsodium23 \
        libx264-164 libvpx9 libopus0 libmp3lame0 libvorbis0a libvorbisenc2 libdav1d7 \
        net-tools nmap openssh-client pianobar libpopt0 socat ssocr

# s6-overlay, bashio, upstream's rootfs/ (the /init wrapper and the
# home-assistant service), FFmpeg 8, go2rtc and PicoTTS.
COPY --from=source /s6/ /
COPY --from=source /bashio/ /
COPY --from=source /rootfs/ /
COPY --from=go2rtc_builder /out/go2rtc /bin/go2rtc
COPY --from=ffmpeg_builder /opt/ffmpeg/usr/local/ /usr/local/
COPY --from=picotts_builder /opt/picotts/usr/local/ /usr/local/
RUN mkdir -p /etc/fix-attrs.d /etc/services.d && ldconfig && go2rtc --version && ffmpeg -version

COPY --from=python_builder /opt/venv /opt/venv
COPY --from=python_builder /usr/src/homeassistant /usr/src/homeassistant
COPY --from=python_builder /opt/ha-riscv64 /usr/share/ha-riscv64

WORKDIR /config
VOLUME /config
EXPOSE 8123

# Cold starts on riscv64 hardware are slow: importing Home Assistant and
# setting up the default_config integrations takes minutes on an SBC.
HEALTHCHECK --start-period=600s --interval=60s --timeout=10s \
    CMD curl -fsS -o /dev/null "http://127.0.0.1:8123/" || exit 1

ENTRYPOINT ["/init"]
