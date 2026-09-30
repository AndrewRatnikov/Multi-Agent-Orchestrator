#!/usr/bin/env python3
"""
autofix.py: run the target repo's auto-fixer (usually `eslint --fix`) on the
files the Tester or Coder just produced, before they are committed.

Called by the orchestrator right after it copies runs/{RUN_ID}/<tests|code>/ into
{REPO} and before `git add`. Fixed files are copied back into runs/{RUN_ID}/ so the
run folder and the repo stay identical (the sandbox and the reviewer read the run
folder). Best effort: it never blocks the pipeline. Whatever it can't fix is
caught later by Verify.

Usage:
  autofix.py <repo_path> <run_id> tests|code

Command resolution:
  1. "autofix" in {REPO}/.claude/verify.json as it exists on the run's ORIGINAL
     branch (so a run can't change what gets executed). "{files}" is replaced by the
     quoted file list. Set it to null to turn autofix off for a repo.
  2. Otherwise, if {REPO}/node_modules/.bin/eslint exists: eslint --fix {files}
  3. Otherwise: nothing to do.

Exit: always 0 (prints what it did), except 3 for usage errors.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LINTABLE = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue")


def read_state(run_dir):
    state = {}
    path = os.path.join(run_dir, "state.md")
    if os.path.exists(path):
        for line in open(path):
            if ":" in line:
                k, v = line.split(":", 1)
                state.setdefault(k.strip(), v.strip())
    return state


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def resolve_command(repo, original):
    code, raw = git(repo, "show", "{}:.claude/verify.json".format(original))
    if code == 0 and raw.strip().startswith("{"):
        cfg = json.loads(raw)
        if "autofix" in cfg:
            return cfg["autofix"] or None, "verify.json"
    if os.path.exists(os.path.join(repo, "node_modules", ".bin", "eslint")):
        return "node_modules/.bin/eslint --fix {files}", "default (eslint found)"
    return None, "no autofix configured and no local eslint"


def main():
    if len(sys.argv) != 4 or sys.argv[3] not in ("tests", "code"):
        print(__doc__)
        sys.exit(3)
    repo, run_id, mode = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    src_root = os.path.join(run_dir, mode)
    original = read_state(run_dir).get("original_branch", "main")

    files = []
    for dirpath, _, names in os.walk(src_root):
        for n in names:
            if n.endswith(LINTABLE):
                files.append(os.path.relpath(os.path.join(dirpath, n), src_root).replace(os.sep, "/"))
    files.sort()
    if not files:
        print("autofix: no lintable {} files".format(mode))
        return

    cmd_tpl, source = resolve_command(repo, original)
    if not cmd_tpl:
        print("autofix: skipped ({})".format(source))
        return

    # Only fix files that are exactly what the run produced (the orchestrator just copied them).
    targets = []
    for f in files:
        in_repo = os.path.join(repo, f)
        if os.path.exists(in_repo) and open(in_repo, "rb").read() == open(os.path.join(src_root, f), "rb").read():
            targets.append(f)
        else:
            print("autofix: skipping {} (not copied into the repo yet, or differs from the run folder)".format(f))
    if not targets:
        return

    before = git(repo, "status", "--porcelain")[1]
    cmd = cmd_tpl.replace("{files}", " ".join(shlex.quote(f) for f in targets))
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        out, rc = r.stdout.decode("utf-8", "replace"), r.returncode
    except subprocess.TimeoutExpired:
        print("autofix: timed out after 300s; continuing without it")
        return

    fixed = []
    for f in targets:
        in_repo = os.path.join(repo, f)
        in_run = os.path.join(src_root, f)
        if open(in_repo, "rb").read() != open(in_run, "rb").read():
            shutil.copy2(in_repo, in_run)
            fixed.append(f)

    after = git(repo, "status", "--porcelain")[1]
    touched_elsewhere = sorted(set(after.splitlines()) - set(before.splitlines()) -
                               {l for l in after.splitlines() if l[3:] in targets})
    print("autofix ({}): `{}`".format(source, cmd_tpl))
    print("autofix: fixed {} of {} file(s){}".format(len(fixed), len(targets), ": " + ", ".join(fixed) if fixed else ""))
    if rc not in (0, 1):
        # eslint: 1 = unfixable problems remain (fine, Verify reports them); 2 = crash/config error.
        print("autofix: command exited {} (not a verdict; Verify still checks everything). Last output:".format(rc))
        print("\n".join(out.strip().splitlines()[-15:]))
    if touched_elsewhere:
        print("autofix: WARNING: the command also changed files outside this run's output; review before committing:")
        print("\n".join("  " + l for l in touched_elsewhere))


if __name__ == "__main__":
    main()
