"""Shared helpers for the riscv64 dependency tooling.

Used by install-requirements.py (image build), plan-wheels.py (CI planning) and
build-wheels.py (wheel shards). Standard library only.
"""
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

PYPI = "https://pypi.org/pypi"
RISE_INDEX = "https://gitlab.com/api/v4/projects/56254198/packages/pypi/simple/"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def req_name(line: str) -> str:
    return norm(re.split(r"[\s=<>!~\[;@]", line, maxsplit=1)[0])


def vkey(v: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", v)]


def log(msg: str, tag: str = "ha-riscv64") -> None:
    print(f"[{tag}] {msg}", flush=True)


def http_get(url: str, retries: int = 3) -> str | None:
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ha-riscv64-build"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    return None


def wheel_tags(filename: str) -> tuple[str, str, str]:
    """(python tag, abi tag, platform tag) of a wheel file name."""
    parts = filename[: -len(".whl")].split("-")
    return parts[-3], parts[-2], parts[-1]


def usable_riscv64_wheel(filename: str, py_minor: int = 14) -> bool:
    """A glibc riscv64 wheel CPython 3.<py_minor> can load."""
    py, abi, plat = wheel_tags(filename)
    if "riscv64" not in plat or "musllinux" in plat:
        return False
    if py == f"cp3{py_minor}":
        return True
    m = re.fullmatch(r"cp3(\d+)", py)
    if m and abi == "abi3" and int(m.group(1)) <= py_minor:
        return True
    return py in ("py3", "py2.py3") and abi == "none"


def rise_wheels(package: str) -> list[str]:
    """riscv64 wheel file names of `package` on the RISE index."""
    html = http_get(RISE_INDEX + package + "/")
    if not html:
        return []
    return sorted(set(re.findall(r"([A-Za-z0-9_.]+-[0-9][^\"<> /]*\.whl)", html)))


def rise_versions(package: str, py_minor: int = 14) -> list[str]:
    """Versions of `package` with a riscv64 wheel usable by CPython 3.14 on RISE."""
    versions = {
        f.split("-")[1]
        for f in rise_wheels(package)
        if usable_riscv64_wheel(f, py_minor)
    }
    return sorted(versions, key=vkey)


def parse_groups(path: Path) -> list[tuple[str, list[str]]]:
    """[(requirement line, [integration domain, ...])] in file order."""
    groups, domains = [], []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            domains = []
        elif line.startswith("# homeassistant."):
            domains.append(line[2:].split(".")[-1])
        elif line.startswith(("#", "-")):
            continue
        else:
            groups.append((line, domains))
            domains = []
    return groups


def read_skip(path: Path) -> set[str]:
    return {l.split("#")[0].strip() for l in path.read_text().splitlines()} - {""}


def essential_domains(src: Path) -> set[str]:
    """default_config and everything it depends on, transitively."""
    comps = src / "homeassistant" / "components"
    seen, todo = set(), ["default_config"]
    while todo:
        dom = todo.pop()
        if dom in seen:
            continue
        seen.add(dom)
        manifest = comps / dom / "manifest.json"
        if manifest.exists():
            todo += json.loads(manifest.read_text()).get("dependencies", [])
    return seen


def prepare(src: Path, work: Path, substitutes: list[str], tag: str = "ha-riscv64"):
    """Write constraints/overrides/core requirement files into `work`.

    Returns (resolver args, substitution report lines, core requirements path).

    requirements.txt starts with "-c homeassistant/package_constraints.txt",
    which would bring back pins that are substituted below, so a copy without
    it is written and the (possibly edited) constraints are passed explicitly.
    """
    work.mkdir(parents=True, exist_ok=True)
    cons_lines = (src / "homeassistant" / "package_constraints.txt").read_text().splitlines()
    over_lines, report = [], []
    for pkg in substitutes:
        pin = next((l for l in cons_lines if req_name(l) == norm(pkg) and "==" in l), None)
        pinned = pin.split("==")[1].split()[0] if pin else None
        versions = rise_versions(pkg)
        if not versions:
            log(f"no riscv64 wheel for {pkg} on RISE, keeping upstream pin {pinned}", tag)
            continue
        if pinned in versions:
            continue  # exact upstream pin is available prebuilt
        # Prefer a patch release of the pinned minor (numpy 2.3.2 -> 2.3.4);
        # otherwise the newest RISE build below the pin (grpcio 1.83 -> 1.78).
        pk = vkey(pinned) if pinned else None
        same_minor = [v for v in versions if pk and vkey(v)[:2] == pk[:2] and vkey(v) >= pk]
        older = [v for v in versions if pk is None or vkey(v) <= pk]
        chosen = (same_minor or older or versions)[-1]
        cons_lines = [l for l in cons_lines if req_name(l) != norm(pkg)]
        over_lines.append(f"{pkg}=={chosen}")
        report.append(f"{pkg}: upstream {pinned} -> {chosen} (RISE riscv64 wheel)")
        log(report[-1], tag)

    constraints, overrides, core = work / "constraints.txt", work / "overrides.txt", work / "requirements-core.txt"
    constraints.write_text("\n".join(cons_lines) + "\n")
    overrides.write_text("\n".join(over_lines) + "\n")
    core.write_text("".join(
        l + "\n" for l in (src / "requirements.txt").read_text().splitlines()
        if not l.startswith(("-c", "-r"))))
    common = ["-c", str(constraints)] + (["--override", str(overrides)] if over_lines else [])
    return common, report, core
