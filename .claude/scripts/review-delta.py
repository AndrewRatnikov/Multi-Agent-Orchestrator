#!/usr/bin/env python3
"""review-delta.py: re-reviews look only at what changed in the tests.

The Opus Test-Reviewer used to re-read every test file, plan.md and prd.md on every
pass, even when a retry only fixed two lint warnings. After the first full review,
later passes get just the diff of the tests since the last review.

Usage:
  review-delta.py <run_id> <repo> diff
      Before a Stage 4b review. Compares the test files committed in <repo> now with
      the commit the last review saw, and writes runs/<run_id>/review-delta.diff.
      Exit 0  delta review: review-delta.diff has the changes (summary on stdout)
      Exit 1  full review: no earlier review in this run, or the change is too large
              to judge from a diff (reason on stdout)
      Exit 2  no test changes since a review that PASSED: no review needed
      Exit 3  setup error
  review-delta.py <run_id> <repo> mark PASS|FAIL
      After every Stage 4b review: records the commit it reviewed and the verdict
      (runs/<run_id>/.last-test-review.json). On PASS it also stamps
      runs/<run_id>/.test_reviewer_passed_at, the gate Stage 5b checks against.

"Test files" are the files under runs/<run_id>/tests/ (repo-relative), plus any
test file the commits since the last review deleted.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_PATTERN = re.compile(r"(\.|-)(test|spec)\.[cm]?[jt]sx?$|e2e-spec\.[jt]s$|(^|/)(__tests__|__mocks__|tests?)/")
FULL_IF_CHANGED_SHARE = 0.4   # more than this share of all test lines changed: review everything


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError("git {}: {}".format(" ".join(args), r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout.decode("utf-8", "replace")


def test_files(run_dir):
    root = os.path.join(run_dir, "tests")
    out = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n != ".DS_Store":
                out.append(os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/"))
    return sorted(out)


def cmd_mark(run_dir, repo, verdict):
    head = git(repo, "rev-parse", "HEAD").strip()
    with open(os.path.join(run_dir, ".last-test-review.json"), "w") as f:
        json.dump({"commit": head, "verdict": verdict,
                   "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        f.write("\n")
    if verdict == "PASS":
        stamp = os.path.join(run_dir, ".test_reviewer_passed_at")
        with open(stamp, "a"):
            os.utime(stamp, None)
    print("review-delta: recorded {} review of {}{}".format(
        verdict, head[:10], " and stamped .test_reviewer_passed_at" if verdict == "PASS" else ""))
    return 0


def cmd_diff(run_dir, repo):
    last_path = os.path.join(run_dir, ".last-test-review.json")
    if not os.path.isfile(last_path):
        print("review-delta: full review (no earlier review in this run)")
        return 1
    last = json.load(open(last_path))
    base, verdict = last.get("commit"), last.get("verdict")

    files = test_files(run_dir)
    changed = [f for f in git(repo, "diff", "--name-only", base, "HEAD").splitlines()
               if f in files or TEST_PATTERN.search(f)]
    if not changed:
        if verdict == "PASS":
            print("review-delta: no test changes since the PASS review of {}; no review needed".format(base[:10]))
            return 2
        print("review-delta: full review (tests unchanged since a FAIL review; nothing to compare)")
        return 1

    diff = git(repo, "diff", "--unified=8", base, "HEAD", "--", *changed)
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    total = 0
    for f in files:
        try:
            with open(os.path.join(run_dir, "tests", f), encoding="utf-8", errors="replace") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            pass
    share = (added + removed) / max(total, 1)
    if share > FULL_IF_CHANGED_SHARE:
        print("review-delta: full review ({}% of all test lines changed since the last review)".format(round(100 * share)))
        return 1

    with open(os.path.join(run_dir, "review-delta.diff"), "w") as f:
        f.write(diff)
    print("review-delta: delta review: {} test file(s), +{} -{} lines since the {} review of {} "
          "(runs/{}/review-delta.diff)".format(len(changed), added, removed, verdict, base[:10],
                                                os.path.basename(run_dir)))
    return 0


def main():
    a = sys.argv[1:]
    if len(a) < 3 or a[2] not in ("diff", "mark") or (a[2] == "mark" and (len(a) != 4 or a[3] not in ("PASS", "FAIL"))):
        print(__doc__)
        return 3
    run_dir, repo = os.path.join(ORCH_ROOT, "runs", a[0]), os.path.abspath(a[1])
    if not os.path.isdir(run_dir):
        print("ERROR: runs/{}/ not found".format(a[0]), file=sys.stderr)
        return 3
    try:
        return cmd_mark(run_dir, repo, a[3]) if a[2] == "mark" else cmd_diff(run_dir, repo)
    except RuntimeError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
