#!/usr/bin/env python3
"""seed-code.py: put the current version of every file the Coder MODIFIES into
runs/{RUN_ID}/code/ before the Coder starts, so it edits copies in place instead
of reading whole files and writing them back out (the most expensive thing a
run did: a 96 KB file re-typed for a five-line change).

Usage:
  seed-code.py <run_id> <repo>

Files come from contract.json (`files` entries with action "modify" and owner
"coder"); runs without contract.json fall back to plan.md's `## Files changed`
table (non-test rows whose Action is MODIFY).

A file that already exists under code/ is never overwritten: on a retry it is
the Coder's previous version, which is also what is committed in {repo}.

Exit: 0 ok (prints what it did), 3 setup error (a MODIFY file is missing from
the repo, or no file list could be read).
"""
import json
import os
import re
import shutil
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_PATTERN = re.compile(r"(\.|-)(test|spec)\.[cm]?[jt]sx?$|e2e-spec\.[jt]s$|(^|/)(__tests__|__mocks__|tests?)/")


def from_contract(path):
    with open(path) as f:
        data = json.load(f)
    return [e["path"] for e in data.get("files", [])
            if isinstance(e, dict) and e.get("action") == "modify" and e.get("owner") == "coder" and e.get("path")]


def from_plan(path):
    text = open(path).read()
    m = re.search(r"^##\s+Files changed\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return None
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[1].upper() != "MODIFY":
            continue
        p = cells[0]
        while p.startswith("./"):
            p = p[2:]
        if p and not TEST_PATTERN.search(p):
            out.append(p)
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[4], file=sys.stderr)
        return 3
    run_id, repo = sys.argv[1], os.path.abspath(sys.argv[2])
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    contract, plan = os.path.join(run_dir, "contract.json"), os.path.join(run_dir, "plan.md")

    if os.path.isfile(contract):
        files, source = from_contract(contract), "contract.json"
    elif os.path.isfile(plan):
        files, source = from_plan(plan), "plan.md"
    else:
        files, source = None, None
    if files is None:
        print(f"ERROR: no contract.json or plan.md Files changed table in runs/{run_id}/", file=sys.stderr)
        return 3

    code_dir = os.path.join(run_dir, "code")
    seeded, kept, missing, size = [], [], [], 0
    for rel in files:
        src, dst = os.path.join(repo, rel), os.path.join(code_dir, rel)
        if os.path.exists(dst):
            kept.append(rel)
        elif not os.path.isfile(src):
            missing.append(rel)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            seeded.append(rel)
            size += os.path.getsize(dst)

    if missing:
        for rel in missing:
            print(f"MISSING_IN_REPO: {rel} is declared MODIFY but does not exist in {repo}")
        return 3
    print(f"seed-code: {len(seeded)} MODIFY file(s) copied into runs/{run_id}/code/ "
          f"({size // 1024} KB), {len(kept)} already there and kept (from {source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
