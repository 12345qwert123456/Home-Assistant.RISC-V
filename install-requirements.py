#!/usr/bin/env python3
"""Install Home Assistant's dependency set on riscv64.

Upstream installs requirements_all.txt in one `uv pip install --no-build` pass
against wheels it builds itself. On riscv64 a handful of integrations pull in
packages that cannot be built (or that would not fit in a CI time limit), and a
single failure aborts a whole resolver run. This script keeps the build honest
and resumable:

  1. requirements.txt (Home Assistant core) is installed first and is strict:
     any failure fails the build.
  2. requirements_all.txt is split into per-integration groups using the
     "# homeassistant.components.<domain>" markers upstream generates.
     Integrations listed in skip-integrations.txt are left out up front.
  3. The rest is installed in one pass. When a package fails to build, the
     direct requirements that pulled it in are dropped (together with the
     integrations they belong to) and the pass is retried. Wheels that were
     already built come from uv's cache, so a retry is cheap.
  4. Integrations that default_config needs (directly or via `dependencies`)
     are essential: if one of those would have to be dropped, the build fails
     instead of silently shipping a Home Assistant that cannot start.

Dropped integrations are written to <out>/skipped-integrations.txt. Home
Assistant still lists them in the UI; installing one downloads its requirements
at runtime and fails there with a clear message if they need a compiler.

usage: install-requirements.py <ha-source-dir> <skip-file> <out-dir>

Wheels that were pre-built by CI are picked up from UV_FIND_LINKS (see
build-wheels.py); whatever is still missing is compiled here.

Environment:
  RISE_SUBSTITUTES  space-separated packages whose exact upstream pin has no
                    riscv64 wheel but a nearby version does on the RISE index
                    (see Dockerfile); the pin is replaced by that version.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from hadeps import essential_domains, log as _log, norm, parse_groups, prepare, read_skip, req_name

MAX_PASSES = 40


def log(msg: str) -> None:
    _log(msg, "install-requirements")


def run_streamed(cmd: list[str]) -> tuple[int, str]:
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = []
    for line in p.stdout:
        sys.stdout.write(line)
        out.append(line)
    return p.wait(), "".join(out)


def via_map(compile_out: str) -> dict[str, list[str]]:
    via, cur = {}, None
    for line in compile_out.splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==", line)
        if m:
            cur = norm(m.group(1))
            via[cur] = []
        elif cur and (m := re.match(r"^\s+# via (.*)", line) or re.match(r"^\s+#\s+(\S.*)", line)):
            via[cur] += [norm(x.strip().split()[0]) for x in m.group(1).split(",") if x.strip() and not x.strip().startswith("-")]
    return via


def main() -> int:
    src, skip_file, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/ha-requirements")

    common, subst_report, core = prepare(src, work, os.environ.get("RISE_SUBSTITUTES", "").split(), "install-requirements")

    # --- 1. core: strict -------------------------------------------------
    rc, _ = run_streamed(["uv", "pip", "install", "-r", str(core), *common])
    if rc:
        log("ERROR: Home Assistant core requirements failed to install")
        return rc

    # --- 2. integrations: best effort -------------------------------------
    skip = read_skip(skip_file)
    essential = essential_domains(src)
    groups = parse_groups(src / "requirements_all.txt")
    keep = [(l, d) for l, d in groups if not (d and set(d) <= skip)]
    log(f"{len(groups)} requirement groups, {len(groups) - len(keep)} dropped by skip list, {len(keep)} to install")

    dropped: dict[str, str] = {d: "skip-integrations.txt" for d in sorted(skip)}
    via = None
    for n in range(1, MAX_PASSES + 1):
        req_file = work / "requirements-extra.txt"
        req_file.write_text("\n".join(l for l, _ in keep) + "\n")
        log(f"pass {n}: {len(keep)} requirements")
        rc, text = run_streamed(["uv", "pip", "install", "-r", str(req_file), *common])
        if rc == 0:
            break
        failed = sorted({norm(m.split("==")[0]) for m in re.findall(r"Failed to build `([^`]+)`", text)})
        if not failed:
            log("ERROR: install failed, and not because a package failed to build:")
            return rc
        if via is None:
            comp = subprocess.run(["uv", "pip", "compile", str(req_file), *common, "--no-header", "--quiet"],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            via = via_map(comp.stdout)
        direct = {req_name(l): (l, d) for l, d in keep}

        def roots(p: str, seen: set[str]) -> set[str]:
            if p in seen:
                return set()
            seen.add(p)
            r = {p} if p in direct else set()
            for parent in via.get(p, []):
                r |= roots(parent, seen)
            return r

        victims = set()
        for pkg in failed:
            victims |= roots(pkg, set())
        if not victims:
            log(f"ERROR: cannot trace {failed} back to a requirement")
            return 1
        for v in sorted(victims):
            line, doms = direct[v]
            bad = [d for d in doms if d in essential]
            if bad:
                log(f"ERROR: essential integration(s) {bad} need {line}, which failed to build ({failed})")
                return 1
            for d in doms or [v]:
                dropped[d] = f"{line} (via {', '.join(failed)})"
            log(f"dropping {line} <- failed: {', '.join(failed)}")
        keep = [(l, d) for l, d in keep if req_name(l) not in victims]
    else:
        log("ERROR: too many passes")
        return 1

    # --- report ---------------------------------------------------------
    (out / "skipped-integrations.txt").write_text(
        "".join(f"{d}\t{why}\n" for d, why in sorted(dropped.items())))
    (out / "substitutes.txt").write_text("".join(s + "\n" for s in subst_report))
    log(f"done. dropped by build failures: {len(dropped)}; substitutes: {len(subst_report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
