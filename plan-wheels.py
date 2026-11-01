#!/usr/bin/env python3
"""Work out which dependencies have to be compiled for riscv64, and split them
into shards that CI can build in parallel.

Runs natively on the CI runner (no QEMU, no riscv64): `uv pip compile` resolves
the complete pinned dependency closure for the riscv64 platform, and every
pinned package is checked against PyPI and the RISE wheel index. A package
needs a source build when neither has a wheel that CPython 3.14 on glibc/riscv64
can load and it is not a pure-Python wheel.

usage: plan-wheels.py <ha-source-dir> <skip-file> <out-dir>
                      [--have FILE] [--shards N] [--python 3.14]

  --have FILE   file with one wheel file name per line (already built, e.g. the
                assets of the wheels release); those packages are left out
  --shards N    maximum number of shards (default 4)

Writes <out-dir>/plan.json and prints a summary. Environment:
  RISE_SUBSTITUTES  same meaning as for install-requirements.py
"""
import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hadeps import (PYPI, http_get, log as _log, norm, parse_groups, prepare, read_skip,
                    rise_wheels, usable_riscv64_wheel, wheel_tags)

WEIGHTS_FILE = Path(__file__).with_name("wheel-weights.txt")


def log(msg: str) -> None:
    _log(msg, "plan-wheels")


def load_weights() -> dict[str, float]:
    weights = {}
    if WEIGHTS_FILE.exists():
        for line in WEIGHTS_FILE.read_text().splitlines():
            line = line.split("#")[0].split()
            if len(line) == 2:
                weights[norm(line[0])] = float(line[1])
    return weights


def load_prefer_source() -> set[str]:
    path = Path(__file__).with_name("prefer-source.txt")
    return {norm(l.split("#")[0].strip()) for l in path.read_text().splitlines() if l.split("#")[0].strip()} if path.exists() else set()


def classify(item: tuple[str, str], py_minor: int, prefer_source: set[str]) -> tuple[str, str, str]:
    """-> (name, version, class) with class in
    pure | riscv64 | rise | pure-fallback | build | unbuildable | unknown

    "build" means: compile from the sdist. That includes packages that also
    publish a pure-Python wheel when prefer-source.txt lists them, because the
    pure wheel would otherwise win and run without the C speedups."""
    name, ver = item
    data = http_get(f"{PYPI}/{name}/{ver}/json")
    if data is None:
        return name, ver, "unknown"
    files = json.loads(data)["urls"]
    wheels = [f["filename"] for f in files if f["filename"].endswith(".whl")]
    has_sdist = any(not f["filename"].endswith(".whl") for f in files)
    if any(usable_riscv64_wheel(w, py_minor) for w in wheels):
        return name, ver, "riscv64"
    pure = [w for w in wheels if wheel_tags(w)[2] == "any" and wheel_tags(w)[0].startswith(("py3", "py2.py3"))]
    if pure:
        # A package that publishes both a pure wheel and compiled wheels would
        # silently run its slow pure-Python fallback here.
        if not [w for w in wheels if "x86_64" in wheel_tags(w)[2]]:
            return name, ver, "pure"
        if any(f.split("-")[1] == ver and usable_riscv64_wheel(f, py_minor) for f in rise_wheels(name)):
            return name, ver, "rise"
        return name, ver, "build" if name in prefer_source and has_sdist else "pure-fallback"
    if any(f.split("-")[1] == ver and usable_riscv64_wheel(f, py_minor) for f in rise_wheels(name)):
        return name, ver, "rise"
    return name, ver, "build" if has_sdist else "unbuildable"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("skip_file", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--have", type=Path)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--python", default="3.14")
    args = ap.parse_args()
    py_minor = int(args.python.split(".")[1])
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.out / "work"

    common, subst, core = prepare(args.src, work, os.environ.get("RISE_SUBSTITUTES", "").split(), "plan-wheels")

    skip = read_skip(args.skip_file)
    groups = parse_groups(args.src / "requirements_all.txt")
    extras = work / "requirements-extra.txt"
    extras.write_text("\n".join(l for l, d in groups if not (d and set(d) <= skip)) + "\n")

    closure = work / "closure.txt"
    cmd = ["uv", "pip", "compile", str(core), str(extras), *common,
           "--python-version", args.python, "--python-platform", "riscv64-unknown-linux",
           "--no-annotate", "--no-header", "-o", str(closure)]
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode:
        log("ERROR: could not resolve the dependency closure")
        return r.returncode

    pins = []
    for line in closure.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line)
        if m:
            pins.append((norm(m.group(1)), m.group(2)))
    log(f"{len(pins)} pinned packages in the closure")

    have = set()
    if args.have and args.have.exists():
        for f in args.have.read_text().split():
            if f.endswith(".whl"):
                n, v = f.split("-")[:2]
                have.add((norm(n), v))

    with ThreadPoolExecutor(24) as ex:
        prefer_source = load_prefer_source()
        results = list(ex.map(lambda p: classify(p, py_minor, prefer_source), pins))
    by_class: dict[str, list[str]] = {}
    for n, v, c in results:
        by_class.setdefault(c, []).append(f"{n}=={v}")

    todo = [(n, v) for n, v, c in results if c == "build" and (n, v) not in have]
    weights = load_weights()
    todo.sort(key=lambda p: -weights.get(p[0], 1.0))
    n_shards = max(1, min(args.shards, len(todo)))
    shards: list[list[str]] = [[] for _ in range(n_shards)]
    load = [0.0] * n_shards
    for n, v in todo:  # greedy: heaviest package onto the lightest shard
        i = load.index(min(load))
        shards[i].append(f"{n}=={v}")
        load[i] += weights.get(n, 1.0)
    shards = [s for s in shards if s]

    plan = {
        "python": args.python,
        "substitutes": subst,
        "shards": shards,
        "already_built": sorted(f"{n}=={v}" for n, v, c in results if c == "build" and (n, v) in have),
        "classes": {c: sorted(v) for c, v in by_class.items() if c in ("pure-fallback", "unbuildable", "unknown", "rise")},
        "counts": {c: len(v) for c, v in by_class.items()},
    }
    (args.out / "plan.json").write_text(json.dumps(plan, indent=1))
    log("closure by class: " + ", ".join(f"{c}={n}" for c, n in sorted(plan["counts"].items())))
    log(f"to compile: {len(todo)} packages in {len(shards)} shard(s); already built: {len(plan['already_built'])}")
    for c in ("unbuildable", "unknown"):
        if plan["classes"].get(c):
            log(f"WARNING {c}: {', '.join(plan['classes'][c])}")
    if plan["classes"].get("pure-fallback"):
        log("pure-Python fallbacks in use: " + ", ".join(plan["classes"]["pure-fallback"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
