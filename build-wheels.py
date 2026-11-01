#!/usr/bin/env python3
"""Compile one shard of the wheels listed by plan-wheels.py (runs on riscv64).

Every package is built on its own with `pip wheel --no-deps`, so one failing
package never costs the others, and the wheels land in <out-dir> as soon as
each finishes. A deadline stops new builds from starting once the CI job is
close to its time limit: whatever finished is uploaded by the workflow, and the
next run picks up where this one stopped (plan-wheels.py skips packages that
are already built).

usage: build-wheels.py <plan.json> <shard-index> <out-dir>
                       [--jobs N] [--deadline-minutes M]

Writes <out-dir>/BUILD-REPORT.txt and, for failures, <out-dir>/failures/<pkg>.log
(the report and logs are not wheels: upload only *.whl).
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hadeps import log as _log, norm


def log(msg: str) -> None:
    _log(msg, "build-wheels")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", type=Path)
    ap.add_argument("shard", type=int)
    ap.add_argument("out", type=Path)
    ap.add_argument("--jobs", type=int, default=2, help="packages built at the same time")
    ap.add_argument("--deadline-minutes", type=float, default=300)
    args = ap.parse_args()

    specs = json.loads(args.plan.read_text())["shards"][args.shard]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "failures").mkdir(exist_ok=True)
    start = time.monotonic()
    deadline = start + args.deadline_minutes * 60
    done, failed, deferred = [], [], []

    def build(spec: str) -> None:
        if time.monotonic() > deadline:
            deferred.append(spec)
            log(f"deadline reached, deferring {spec}")
            return
        t0 = time.monotonic()
        log(f"building {spec}")
        logfile = args.out / "failures" / f"{norm(spec.split('==')[0])}.log"
        # hard stop 20 minutes after the deadline so the workflow can still upload
        limit = max(300, deadline - time.monotonic() + 1200)
        with open(logfile, "w") as lf:
            try:
                rc = subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir",
                     "--disable-pip-version-check", "--no-binary", norm(spec.split("==")[0]),
                     "-w", str(args.out), spec],
                    stdout=lf, stderr=subprocess.STDOUT, text=True, timeout=limit).returncode
            except subprocess.TimeoutExpired:
                lf.write("\nTIMEOUT\n")
                rc = 124
        mins = (time.monotonic() - t0) / 60
        if rc == 0:
            logfile.unlink()
            done.append((spec, mins))
            log(f"built {spec} in {mins:.1f} min")
        else:
            failed.append((spec, mins))
            log(f"FAILED {spec} after {mins:.1f} min (log: failures/{norm(spec.split('==')[0])}.log)")

    with ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(build, specs))

    report = [f"shard {args.shard}: {len(done)} built, {len(failed)} failed, {len(deferred)} deferred, "
              f"{(time.monotonic() - start) / 60:.1f} min"]
    report += [f"built    {s}  {m:.1f} min" for s, m in sorted(done)]
    report += [f"FAILED   {s}  {m:.1f} min" for s, m in sorted(failed)]
    report += [f"deferred {s}" for s in sorted(deferred)]
    (args.out / "BUILD-REPORT.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
