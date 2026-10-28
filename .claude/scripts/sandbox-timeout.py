#!/usr/bin/env python3
"""sandbox-timeout.py: how long the Stage 6 sandbox may run this repo's tests.

A fixed 120s stopped fitting: minima-spend's suite grew from 31s to 115s in a week,
and a false TIMEOUT sends the run back to a full Opus test review for nothing.

Usage:
  sandbox-timeout.py <repo> <run_id>
Prints one line: "<seconds>\t<reason>". run-tests.sh calls it when no timeout is given.

Resolution:
  1. "sandbox_timeout" (seconds) in {REPO}/.claude/verify.json on the run's original
     branch: a repo can pin its own value.
  2. Otherwise twice the slowest of the last 5 completed sandbox runs of this repo
     (every run folder whose state.md has the same repo), rounded up to 30s and kept
     within 120..900s. Durations come from the "# sandbox: wall Ns exit E" footer
     run-tests.sh writes into each log, or, for older logs, from the test runner's own
     summary (vitest "Duration", jest "Time:", pytest "in Ns"). Timed-out runs don't count.
  3. Otherwise 120s (first run in a repo).
Always exits 0; on any problem it falls back to 120.
"""
import glob
import json
import math
import os
import re
import subprocess
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT, LOW, HIGH, FACTOR, LAST_N = 120, 120, 900, 2, 5
FOOTER = re.compile(r"^# sandbox: wall (\d+)s exit (\d+)", re.M)
RUNNER = [
    re.compile(r"^\s*Duration\s+([\d.]+)\s*(m?s)\b", re.M),             # vitest
    re.compile(r"^\s*Time:\s+([\d.]+)\s*(m?s)\b", re.M),                 # jest
    re.compile(r"=+ .*\bin ([\d.]+)s\b.*=+\s*$", re.M),                  # pytest
]


def read_state(run_dir):
    state = {}
    try:
        for line in open(os.path.join(run_dir, "state.md")):
            if ":" in line:
                k, v = line.split(":", 1)
                state.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return state


def log_seconds(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = FOOTER.search(text)
    if m:
        return None if m.group(2) == "124" else int(m.group(1))
    for rx in RUNNER:
        found = rx.findall(text)
        if found:
            last = found[-1]
            value, unit = (last if isinstance(last, tuple) else (last, "s"))
            secs = float(value) / (1000 if unit == "ms" else 1)
            return int(math.ceil(secs))
    return None


def pinned(repo, original):
    r = subprocess.run(["git", "-C", repo, "show", "{}:.claude/verify.json".format(original)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return None
    try:
        value = json.loads(r.stdout.decode("utf-8", "replace")).get("sandbox_timeout")
        return int(value) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


def resolve(repo, run_id):
    repo = os.path.realpath(repo)
    original = read_state(os.path.join(ORCH_ROOT, "runs", run_id)).get("original_branch") or "main"
    p = pinned(repo, original)
    if p:
        return p, "verify.json sandbox_timeout"

    logs = []
    for run_dir in glob.glob(os.path.join(ORCH_ROOT, "runs", "*")):
        rp = read_state(run_dir).get("repo")
        if not rp or os.path.realpath(rp) != repo:
            continue
        logs += glob.glob(os.path.join(run_dir, "sandbox-logs", "*.log"))
    # Log names are UTC timestamps (YYYYmmddTHHMMSSZ.log): sort by name, newest last.
    logs.sort(key=os.path.basename)
    durations = [d for d in (log_seconds(p) for p in logs) if d is not None][-LAST_N:]
    if not durations:
        return DEFAULT, "default (no completed earlier runs of this repo)"
    slowest = max(durations)
    secs = int(math.ceil(slowest * FACTOR / 30.0) * 30)
    secs = max(LOW, min(HIGH, secs))
    return secs, "{}x the slowest of the last {} runs of this repo ({}s), within {}-{}s".format(
        FACTOR, len(durations), slowest, LOW, HIGH)


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        print("{}\tdefault (usage error)".format(DEFAULT))
        return 0
    try:
        secs, why = resolve(sys.argv[1], sys.argv[2])
    except Exception as e:  # never block the sandbox over a timeout lookup
        secs, why = DEFAULT, "default (lookup failed: {})".format(e)
    print("{}\t{}".format(secs, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
