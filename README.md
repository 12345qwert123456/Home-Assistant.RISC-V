# Home Assistant Core on RISC-V (riscv64)

![Platform](https://img.shields.io/badge/platform-linux%2Friscv64-blue)
![Base](https://img.shields.io/badge/base-debian%20trixie-a80030)
![License](https://img.shields.io/badge/license-MIT-green)

Community Docker image for running [Home Assistant Core](https://github.com/home-assistant/core)
on `linux/riscv64` hardware (OrangePi RV2, StarFive VisionFive 2, Milk-V, QEMU riscv64).

The official `ghcr.io/home-assistant/home-assistant` image publishes only `linux/amd64` and
`linux/arm64`, and a straight rebuild for `riscv64` fails in several independent places at once —
the Alpine/musl base image, the wheel index the build installs from, and the `go2rtc` binary each
have their own blocker (see [Why not upstream](#why-not-upstream)). This image works around all of
them, with **no patches to Home Assistant**: the sources are the tagged upstream release, the
`rootfs/` (s6-overlay `/init` and the `home-assistant` service) is upstream's, unmodified.

New Home Assistant releases are detected daily and built automatically.

## Quick start

```bash
docker pull 12345qwert123456/home-assistant-riscv64:latest
```

```bash
mkdir -p ~/ha-config
docker run -d --name homeassistant \
  --restart unless-stopped \
  --privileged \
  --network=host \
  -e TZ=Europe/Berlin \
  -v ~/ha-config:/config \
  -v /run/dbus:/run/dbus:ro \
  12345qwert123456/home-assistant-riscv64:latest
```

Open **http://localhost:8123** and go through onboarding. These are the same run options as the
[official container install](https://www.home-assistant.io/installation/linux#install-home-assistant-container):
`--network=host` for discovery (mDNS/SSDP), `--privileged` for USB/Bluetooth/serial devices,
`/config` for your configuration and database.

> The **first start is slow** on riscv64 hardware — importing Home Assistant and creating the
> database takes several minutes on an SBC. The healthcheck allows 10 minutes before it starts
> counting failures.

### Docker Compose

```yaml
services:
  homeassistant:
    image: 12345qwert123456/home-assistant-riscv64:latest
    container_name: homeassistant
    restart: unless-stopped
    privileged: true
    network_mode: host
    environment:
      TZ: Europe/Berlin
    volumes:
      - ./ha-config:/config
      - /run/dbus:/run/dbus:ro
```

## How it works

The build has several stages; only the ones that must run riscv64 code do.

| Stage | Base image | Runs on | Purpose |
|-------|------------|---------|---------|
| `source` | `debian:trixie-slim` | build host | Downloads the tagged Home Assistant sources, upstream's `rootfs/`, s6-overlay (riscv64 tarball, checksum-verified) and bashio |
| `go2rtc_builder` | `golang:1.25-trixie` | build host | Cross-compiles `go2rtc` for riscv64 (pure Go, `CGO_ENABLED=0`) |
| `ffmpeg_builder` | `debian:trixie` | build host | Cross-compiles FFmpeg 8 (PyAV 19 needs 8.x; trixie has 7.1) with Debian's riscv64 `-dev` codec libraries |
| `picotts_builder` | `python:3.14-slim-trixie` | riscv64 | Builds PicoTTS from the same pinned commit as `home-assistant/docker` |
| `builder_base` → `wheel_builder` / `python_builder` | `python:3.14-slim-trixie` | riscv64 | Toolchain and uv; then either compiles wheels (CI shards) or installs the dependency set into `/opt/venv` |
| final | `python:3.14-slim-trixie` | riscv64 | Runtime packages, s6-overlay, `/opt/venv` and `/usr/src/homeassistant` |

### The Python builder

Home Assistant 2026.10 requires Python ≥ 3.14.2. Debian trixie ships 3.13, so the image is based on
the official `python:3.14-slim-trixie` (which has a riscv64 manifest) instead of on Debian's Python.

Upstream installs `requirements_all.txt` with `uv pip install --no-build` from wheels it builds
into its own index for musllinux. Here the dependency set is installed by
[`install-requirements.py`](install-requirements.py), which sources wheels from:

1. **PyPI** — pure-Python wheels, plus the growing set of riscv64 wheels
   (`lxml`, `jiter`, `pydantic-core`, `msgpack`, `markupsafe`, `greenlet`, `uv`, …).
2. **The [RISE](https://riseproject.gitlab.io/python/wheel_builder/) riscv64 wheel index** — tested
   wheels from the RISC-V Software Ecosystem project (`bcrypt`, `ciso8601`, `orjson`, `PyYAML`,
   `Pillow`, `psutil`, `PyNaCl`, `pycares`, `cffi`, `pandas`, `scipy`, …). Both indexes are
   searched for each exact `==` pin (`UV_INDEX_STRATEGY=unsafe-best-match`), so the only thing this
   changes is *where* a wheel comes from.
3. **Source**, for everything else — with Debian's `-dev` packages and a current Rust from rustup
   (`cryptography`, `zeroconf`, `annotatedyaml`, `ulid-transform`, `fnv-hash-fast`, `lru-dict`,
   `av`, …). The C and Rust toolchains are only in the builder stage; the final image has none.

`install-requirements.py` is strict about Home Assistant core (`requirements.txt`) and **best-effort
about integrations**: if a package for an optional integration cannot be built, the requirements
that pulled it in are dropped together with the integrations they serve, and the install is
retried (wheels already built come from uv's cache). Integrations that `default_config` needs
directly or through `dependencies` are *essential* — if one of those cannot be built, the build
fails instead of shipping a Home Assistant that cannot start.

What was left out, and which pins were substituted, is recorded inside the image:

```bash
docker run --rm --entrypoint cat 12345qwert123456/home-assistant-riscv64 /usr/share/ha-riscv64/skipped-integrations.txt
docker run --rm --entrypoint cat 12345qwert123456/home-assistant-riscv64 /usr/share/ha-riscv64/substitutes.txt
```

#### The one pin that is swapped for a pre-built release

Home Assistant pins every dependency with `==`, and checks the installed version of every
*integration manifest* requirement against its pin at runtime (a mismatch makes it try to install
the pinned version, which fails without a compiler — a swapped `numpy` took down `stream`, and with it
`default_config`, in testing). So pins are kept exact, with one exception:

| Package | Used by | Upstream pin | This image |
|---------|---------|--------------|------------|
| `grpcio` | transitively, by `cloud` and the Google, Nest, Tesla, Starlink integrations | `1.83.1` | newest RISE riscv64 release below the pin |

No integration manifest pins `grpcio`, and compiling it is a multi-hour build. The swap is computed at
build time by `install-requirements.py` (`RISE_SUBSTITUTES`), so it follows whatever RISE has built,
and is recorded in `/usr/share/ha-riscv64/substitutes.txt` and in the GitHub Release notes. To compile
the exact upstream version instead, build with `--build-arg RISE_SUBSTITUTES=""` (a dedicated CI shard,
or a native riscv64 runner, is the realistic place to do that).

### Fitting in GitHub's 6-hour limit

A GitHub-hosted job stops after 6 hours, and compiling ~180 packages without a riscv64 wheel
(`cryptography`, `numpy`, `av`, `awscrt`, the Rust-based ones, …) under QEMU on a 4-vCPU runner does
not fit. So the pipeline is split (`.github/workflows/build-riscv64.yml`):

```
detect ─▶ plan ─▶ wheels (shard 0 … N, parallel) ─▶ image
```

- **`plan`** (native, no QEMU, ~2 min) resolves the complete pinned closure for riscv64 with
  `uv pip compile --python-platform riscv64-unknown-linux` and checks every package against PyPI and
  RISE ([`plan-wheels.py`](plan-wheels.py)). Packages that need a source build are balanced over
  shards using the rough costs in [`wheel-weights.txt`](wheel-weights.txt), minus anything already in
  the `wheels-py314-riscv64` GitHub release.
- **`wheels`** runs one job per shard, in parallel, each with its own 6-hour budget
  ([`build-wheels.py`](build-wheels.py) on the `wheel_builder` Dockerfile target). Each package is
  built on its own, so one failure costs nothing else, and **every finished wheel is uploaded even if
  the job stops at its deadline**. The next run — or the next Home Assistant release, whose pins
  mostly repeat — only builds what is still missing.
- **`image`** installs those wheels (no compiler involved), builds the final image and runs the
  smoke tests. It first checks that the wheelhouse is complete and fails fast otherwise, so a
  half-finished compile never turns into a timed-out image build; re-running the workflow resumes it.

Beyond that, the expensive work is kept off QEMU entirely: FFmpeg 8, `go2rtc`, the Home Assistant
sources and s6-overlay are cross-compiled or downloaded on the build host. After the first run the
whole pipeline is dominated by the image job's `apt`/`uv` installs and the one-off byte-compile.

### The runtime image

`python:3.14-slim-trixie` plus the Debian equivalents of the `apk` packages in
[`home-assistant/docker`](https://github.com/home-assistant/docker) (`bluez`, `ffmpeg`, `nmap`,
`iperf3`, `socat`, `pianobar`, `ssocr`, `libturbojpeg0`, `libzbar0t64`, …), s6-overlay, bashio,
go2rtc and PicoTTS. The entrypoint is upstream's `/init`, so the container behaves like the official
one: same `/config` volume, same port 8123, same graceful-shutdown timeout.

## Build

The **same Dockerfile builds both natively on a riscv64 board and under QEMU**.

### On an x86_64 / arm64 machine (QEMU)

```bash
# one-time: register the riscv64 emulator (Docker Desktop already ships it)
docker run --privileged --rm tonistiigi/binfmt --install riscv64

docker buildx build --platform linux/riscv64 -t home-assistant:riscv64 --load .
```

### Native build on riscv64 hardware

```bash
docker build -t home-assistant:riscv64 .
```

### Build arguments

| Argument | Default | Meaning |
|----------|---------|---------|
| `HA_VERSION` | see `Dockerfile` | Home Assistant release tag |
| `GO2RTC_VERSION` | see `Dockerfile` | Must match the `go2rtc` version in upstream's `Dockerfile` at that tag |
| `PYTHON_IMAGE` | `python:3.14-slim-trixie` | Must satisfy `requires-python` of that Home Assistant release |
| `BUILD_JOBS` | `4` | Parallel compile jobs. Peak memory scales with it — lower it if the build is OOM-killed |
| `RISE_SUBSTITUTES` | `grpcio` | Packages whose pin may be swapped for a pre-built RISE release (empty = compile the exact pin) |
| `FFMPEG_VERSION` | see `Dockerfile` | FFmpeg release cross-compiled for PyAV and the `ffmpeg` binary. PyAV 19 needs FFmpeg 8.x |

To keep an integration out of the image, add its domain to
[`skip-integrations.txt`](skip-integrations.txt). Without a `wheelhouse/` the Dockerfile compiles
everything itself — correct, but slow (see [above](#fitting-in-githubs-6-hour-limit)). To pre-build
wheels locally, build the `wheel_builder` target and run it on a shard of `plan-wheels.py`'s output:

```bash
docker buildx build --platform linux/riscv64 --target wheel_builder -t wheel-builder --load .
python plan-wheels.py ha-src skip-integrations.txt plan          # needs uv and Python 3.14
docker run --rm --platform linux/riscv64 -v $PWD/plan:/plan:ro -v $PWD/wheelhouse:/out \
    wheel-builder /plan/plan.json 0 /out
```

## Known limitations

- **Supervisor / Add-ons / Home Assistant OS are not available.** This is the *Container*
  installation method. Install add-on equivalents as separate containers.
- **Integrations that need a compiler at runtime fail to install.** Home Assistant installs an
  integration's requirements on demand; packages with a riscv64 wheel (PyPI or RISE, via
  `UV_EXTRA_INDEX_URL`) install fine, packages that would have to be compiled do not, because the
  runtime image has no toolchain. The list of such integrations that were already attempted at build
  time is in `/usr/share/ha-riscv64/skipped-integrations.txt`.
- **No `libcec` Python bindings and no Telldus.** Upstream builds both from source in its base image;
  `libcec` has no usable Python binding build here and `libtelldus-core` is not packaged for
  Debian trixie.
- **A few libraries run their pure-Python fallback.** Where a package publishes both a compiled and a
  pure-Python wheel and no riscv64 wheel exists, the pure-Python one is used: correct, but slower than
  on amd64/arm64. The ones that matter for speed (`SQLAlchemy`, `websockets`, `cbor2`) are compiled from
  source, see [`prefer-source.txt`](prefer-source.txt); the rest (`charset-normalizer`, `protobuf`,
  `tomli`, …) are not, and `plan-wheels.py` lists them in its log.
- **Performance.** A first start takes minutes on an SBC and the recorder database benefits a lot
  from an NVMe/SSD instead of an SD card.
- **Unofficial.** This image is not supported by Nabu Casa or the Home Assistant project. Do not
  report issues you see on riscv64 to upstream without reproducing them elsewhere.

## Why not upstream?

The blockers for an official riscv64 image, in the order a rebuild hits them:

1. **No riscv64 base image.** `Dockerfile` is `FROM ${BUILD_FROM}` =
   `ghcr.io/home-assistant/base-python`, published for `amd64` and `arm64` only, and built on Alpine.
2. **`uv pip install --no-build` has nothing to install.** Every compiled dependency must come as a
   wheel; upstream's own index (`wheels.home-assistant.io/musllinux-index`) serves only musllinux
   `amd64`/`aarch64` builds.
3. **`go2rtc` is copied from an image with no riscv64 manifest**, and the project publishes no
   riscv64 release asset (it is pure Go, so this image cross-compiles it).
4. **Debian's Python and FFmpeg are too old.** Home Assistant requires Python ≥ 3.14.2 (trixie has 3.13) and its pinned PyAV 19 needs FFmpeg 8 (trixie has 7.1).
5. **~180 pinned packages have no riscv64 wheel** (compiled in CI, see
   [above](#fitting-in-githubs-6-hour-limit)), and `grpcio` is too slow to compile at all
   (see [above](#the-one-pin-that-is-swapped-for-a-pre-built-release)).

## CI/CD

The GitHub Actions workflow (`.github/workflows/build-riscv64.yml`):

- runs daily at 06:00 UTC (and on manual trigger)
- **detect**: reads the latest stable Home Assistant release, the `go2rtc` version from upstream's
  `Dockerfile` and the Python minor from `.python-version` at that tag; skips if a
  `<version>-riscv64` GitHub Release already exists
- **plan / wheels**: compiles missing wheels in parallel shards and caches them in the
  `wheels-py314-riscv64` release (see [above](#fitting-in-githubs-6-hour-limit))
- **image**: builds the `linux/riscv64` image under QEMU and smoke-tests it: riscv64 architecture,
  the installed version equals the tag, the C extensions with slow pure-Python fallbacks really
  loaded, **every `requirements` pin of the `default_config` integrations matches the installed
  version** (the check that catches a swapped pin like `numpy`), `go2rtc`/`ffmpeg`/`nmap` run, and
  **Home Assistant actually starts and answers on port 8123**
- pushes to Docker Hub and creates a GitHub Release listing the swapped pins and left-out integrations

Trigger a specific version, a forced rebuild or a different runner via **workflow_dispatch**.

> The very first run on a repository compiles every wheel, and the longest ones (`cryptography`,
> `numpy`, `av`) are long even on their own shard. A shard that reaches its deadline uploads what it
> finished and the `image` job refuses to start; just re-run the workflow, it resumes. On a
> riscv64 self-hosted runner (an OrangePi RV2 works) pass its label in the `runner` input.

### Required repository settings

| Type | Name | Description |
|------|------|-------------|
| Variable | `DOCKERHUB_USERNAME` | Docker Hub username. If unset, the build and smoke-tests still run; only the push is skipped. |
| Secret | `DOCKERHUB_TOKEN` | Docker Hub access token |

## Versions

| Component | Version |
|-----------|---------|
| Home Assistant Core | latest stable (auto-detected) |
| Python | 3.14 (`python:3.14-slim-trixie`) |
| go2rtc | from upstream `Dockerfile` |
| FFmpeg | 8.1 (cross-compiled, GPL build with x264, VP8/9, Opus, MP3, Vorbis, WebP, dav1d) |
| s6-overlay | 3.2.3.2 |
| Base | `debian:trixie` (riscv64) |
| Rust | current stable (rustup) |

## License

Home Assistant Core is licensed under [Apache License 2.0](https://github.com/home-assistant/core/blob/dev/LICENSE.md).
Build files in this repository are MIT licensed.
