#!/usr/bin/env python3
"""
check-paths.py: before anything is copied into the target repo, check that the
Tester's or Coder's output files are exactly the ones plan.md declares.

The plan's `## Files changed` table is the single list of repo-relative paths
this run may touch, test files included. Files under runs/{RUN_ID}/tests/ and
runs/{RUN_ID}/code/ mirror the repo root, so repo file `tests/foo.test.ts` lives
at runs/{RUN_ID}/tests/tests/foo.test.ts.

Usage:
  check-paths.py <run_id> tests [--prune]
  check-paths.py <run_id> code

tests mode:
  - every file under tests/ must be declared in Files changed      (UNDECLARED_FILE)
  - every declared test file must exist under tests/               (MISSING_FILE)
  --prune: an undeclared file whose name matches a declared file that DOES exist
    (a leftover from an earlier attempt at a different path) is moved to
    archive/strays/ instead of being reported. Anything else is still reported.
code mode:
  - every file under code/ must be declared                         (UNDECLARED_FILE)
  - the Coder must not produce a declared test file                 (TEST_FILE_IN_CODE)
  - every declared non-test file must exist under code/            (MISSING_FILE)

Exit: 0 clean, 1 violations (one per line on stdout), 3 usage/setup error.
"""

import os
import re
import shutil
import sys
from datetime import datetime

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_PATTERN = re.compile(r"(\.|-)(test|spec)\.[cm]?[jt]sx?$|e2e-spec\.[jt]s$|(^|/)(__tests__|__mocks__|tests?)/")


def declared_files(plan_path):
    """Repo-relative paths from the plan's `## Files changed` table (first column)."""
    text = open(plan_path).read()
    m = re.search(r"^##\s+Files changed\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return None
    files = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        path = cells[0].strip("`").strip()
        if path.lower() in ("file", "path") or set(path) <= set("-: "):
            continue
        while path.startswith("./"):
            path = path[2:]
        files.append(path)
    return files


def is_test(path):
    return bool(TEST_PATTERN.search(path))


def files_under(root):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n == ".DS_Store":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/"))
    return sorted(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    prune = "--prune" in sys.argv
    if len(args) != 2 or args[1] not in ("tests", "code"):
        print(__doc__)
        sys.exit(3)
    run_id, mode = args
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    plan = os.path.join(run_dir, "plan.md")
    if not os.path.exists(plan):
        print("SETUP_ERROR: {} not found".format(plan))
        sys.exit(3)
    owners = {}
    contract = os.path.join(run_dir, "contract.json")
    if os.path.exists(contract):
        # contract.json is the source of truth when present: explicit owner per file.
        import json
        try:
            files = json.load(open(contract)).get("files") or []
            owners = {f["path"]: f.get("owner") for f in files if isinstance(f, dict) and f.get("path")}
        except (ValueError, KeyError):
            owners = {}
    declared = list(owners) if owners else declared_files(plan)
    if declared is None:
        print("SETUP_ERROR: plan.md has no `## Files changed` table")
        sys.exit(3)
    declared_set = set(declared)
    if owners:
        global is_test
        is_test = lambda path: owners.get(path) == "tester"  # noqa: E731
    tests_dir, code_dir = os.path.join(run_dir, "tests"), os.path.join(run_dir, "code")
    produced_tests, produced_code = files_under(tests_dir), files_under(code_dir)
    violations, pruned = [], []

    if mode == "tests":
        present = set(produced_tests)
        for f in produced_tests:
            if f in declared_set:
                continue
            base = os.path.basename(f)
            twin = [d for d in declared if os.path.basename(d) == base and d in present]
            if prune and twin:
                dest = os.path.join(run_dir, "archive", "strays-" + datetime.now().strftime("%Y%m%d_%H%M%S"), f)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(os.path.join(tests_dir, f), dest)
                pruned.append("{} (leftover; the declared file is {})".format(f, twin[0]))
                continue
            hint = ""
            same_name = [d for d in declared if os.path.basename(d) == base]
            if same_name:
                hint = " Did you mean {}? Write it to runs/{}/tests/{} (tests/ mirrors the repo root).".format(
                    same_name[0], run_id, same_name[0])
            violations.append("UNDECLARED_FILE tests/{}: not in plan.md's Files changed table.{}".format(f, hint))
        for d in declared:
            if is_test(d) and d not in present and not os.path.exists(os.path.join(code_dir, d)):
                violations.append("MISSING_FILE {}: declared as a test file in Files changed but not under runs/{}/tests/".format(d, run_id))
    else:
        test_set = set(produced_tests)
        for f in produced_code:
            if f not in declared_set:
                violations.append("UNDECLARED_FILE code/{}: not in plan.md's Files changed table".format(f))
            elif f in test_set or is_test(f):
                violations.append("TEST_FILE_IN_CODE code/{}: test files belong to the Tester; the Coder must not write them".format(f))
        present = set(produced_code)
        for d in declared:
            if not is_test(d) and d not in present and d not in test_set:
                violations.append("MISSING_FILE {}: declared in Files changed but not under runs/{}/code/".format(d, run_id))

    for p in pruned:
        print("PRUNED " + p)
    for v in violations:
        print(v)
    if not violations:
        n = len(files_under(tests_dir if mode == "tests" else code_dir))
        print("OK: {} {} file(s) match plan.md".format(n, mode))
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
