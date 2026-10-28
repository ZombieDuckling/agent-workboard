#!/usr/bin/env python3
"""Technical-debt scan for board projects. Runs on the machine that holds the repos.

Usage: python3 debt.py <base64 JSON [{"id": ..., "path": ...}, ...]>  (also works as `python3 - <arg>` over ssh)

Only git-tracked source files count. Each signal carries an estimated fix time; the grade is
debt minutes per 1,000 lines of code, so small and large repos compare fairly. Results are
cached per path and reused until HEAD or the working tree changes.
Stdlib only and Python 3.9 compatible: remote machines run this through `ssh <host> python3 -`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

VERSION = 3
CACHE = Path.home() / ".cache" / "workboard" / "debt-cache.json"
SOURCE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".swift", ".kt", ".kts", ".java", ".go",
    ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".cs", ".vue", ".svelte",
    ".astro", ".sh", ".zsh", ".bash", ".sql", ".dart", ".lua", ".scala", ".ex", ".exs", ".html",
}
SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", ".next", "out", "coverage", "generated",
    "__generated__", "Pods", "third_party", "migrations", ".venv", "venv", "site-packages",
}
SKIP_SUFFIXES = (".min.js", ".d.ts", ".generated.ts", ".pb.go", "_pb2.py")
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|[._-](test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)
MARKER_RE = re.compile(r"(?:#|//|/\*|\*|--|<!--)\s*(TODO|FIXME|HACK|XXX)\b")
SUPPRESS_RE = re.compile(
    r"@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable|biome-ignore|#\s*type:\s*ignore|#\s*noqa"
    r"|#\s*pylint:\s*disable|swiftlint:disable|@SuppressWarnings|//\s*nolint|#\s*nosec"
)
BIG_FILE_LINES = 800
MAX_FILE_BYTES = 1_000_000
TEST_TARGET = 0.2
MINUTES = {"TODO": 15, "XXX": 15, "FIXME": 30, "HACK": 30, "suppression": 10, "big_file": 120, "missing_test": 30}
GRADES = ((30, "A"), (60, "B"), (120, "C"), (240, "D"))


def grade_for(minutes_per_kloc: float) -> str:
    for limit, grade in GRADES:
        if minutes_per_kloc < limit:
            return grade
    return "E"


def git(path: str, *args: str, timeout: int = 20) -> str:
    result = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return result.stdout.decode("utf-8", "replace")


def state_key(path: str) -> str:
    head = git(path, "rev-parse", "HEAD").strip()
    status = git(path, "status", "--porcelain", "--", ".")
    return f"{VERSION}:{head}:{hashlib.sha1(status.encode()).hexdigest()}"


def in_string(line: str, pos: int) -> bool:
    """True when an odd number of quotes precede pos, i.e. the match sits inside a string literal."""
    before = line[:pos].replace("\\\\", "").replace('\\"', "").replace("\\'", "")
    return before.count('"') % 2 == 1 or before.count("'") % 2 == 1 or before.count("`") % 2 == 1


def is_source(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in SKIP_DIRS for part in parts[:-1]) or rel.endswith(SKIP_SUFFIXES):
        return False
    return os.path.splitext(rel)[1].lower() in SOURCE_EXT


def scan(path: str) -> dict:
    files = [f for f in git(path, "ls-files", "-z", "--", ".").split("\0") if f and is_source(f)]
    loc = tests = sources = 0
    markers = {"TODO": 0, "XXX": 0, "FIXME": 0, "HACK": 0}
    suppressions = 0
    big: list = []
    samples: list = []
    for rel in files:
        full = os.path.join(path, rel)
        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        is_test = bool(TEST_RE.search(rel))
        tests += is_test
        sources += not is_test
        loc += sum(1 for line in lines if line.strip())
        if not is_test and len(lines) > BIG_FILE_LINES:
            big.append({"path": rel, "lines": len(lines)})
        for number, line in enumerate(lines, 1):
            match = MARKER_RE.search(line)
            if match and not in_string(line, match.start()):
                markers[match.group(1)] += 1
                if len(samples) < 40:
                    samples.append({"path": rel, "line": number, "kind": match.group(1), "text": line.strip()[:160]})
            match = SUPPRESS_RE.search(line)
            if match and not in_string(line, match.start()):
                suppressions += 1
    missing_tests = max(0, math.ceil(TEST_TARGET * sources) - tests) if sources >= 5 else 0
    big.sort(key=lambda b: -b["lines"])
    parts = {
        "markers": sum(MINUTES[k] * v for k, v in markers.items()),
        "suppressions": MINUTES["suppression"] * suppressions,
        "big_files": MINUTES["big_file"] * len(big),
        "missing_tests": MINUTES["missing_test"] * missing_tests,
    }
    minutes = sum(parts.values())
    per_kloc = round(minutes / (loc / 1000), 1) if loc else 0.0
    samples.sort(key=lambda s: ("FIXME", "HACK", "TODO", "XXX").index(s["kind"]))
    return {
        "loc": loc,
        "source_files": sources,
        "test_files": tests,
        "markers": markers,
        "suppressions": suppressions,
        "big_files": len(big),
        "big_file_list": big[:8],
        "missing_tests": missing_tests,
        "marker_samples": samples[:12],
        "minutes": minutes,
        "minutes_by_kind": parts,
        "per_kloc": per_kloc,
        "grade": grade_for(per_kloc) if loc else None,
    }


def scan_project(path: str, cache: dict) -> dict:
    if not path or not os.path.isdir(path):
        return {"error": "path not found"}
    if os.path.realpath(path) == os.path.realpath(str(Path.home())):
        return {"skipped": "home folder, not a repo"}
    try:
        key = state_key(path)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return {"skipped": "not a git repo"}
    hit = cache.get(path)
    if isinstance(hit, dict) and hit.get("key") == key:
        return hit["result"]
    try:
        result = {**scan(path), "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)[:300]}
    cache[path] = {"key": key, "result": result}
    return result


def main() -> None:
    targets = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8")) if len(sys.argv) > 1 else []
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out = {}
    for target in targets:
        out[target["id"]] = scan_project(target.get("path") or "", cache)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
