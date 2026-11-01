#!/usr/bin/env python3
"""commit-code.py: put the Coder's work into the target repo as logical commits.

Replaces "one commit per file". The Architect plans the commits in contract.json
(`commits`: a message and the files of one coherent piece of work, in build order),
and this script does the whole Stage 5 commit step in one call:

  1. copy runs/<run_id>/code/ into <repo>
  2. autofix.py (lint --fix) and regen.py (rebuild generated files), as before
  3. if a package.json changed: update the lockfile, committed with that package.json
  4. commit: first pass -> one commit per `commits` group, in order, skipping groups with
     no change; fix pass (--fix) -> one commit with everything the pass changed.

Tests are not touched here: Stage 3 commits them first, on their own (test-first).

Usage:
  commit-code.py <repo> <run_id>                       first pass
  commit-code.py <repo> <run_id> --fix "<summary>"     fix pass, e.g. --fix "address sandbox failures (retry 1/2)"
  commit-code.py <repo> <run_id> --lockfile            only update and commit the lockfile (Verify's lockfile hint)

Without a `commits` plan (older runs) the first pass groups by Coder slice
(coder-slices.json) or, failing that, makes one commit for the whole change.

Prints one line per commit. Exit: 0 ok, 1 a git step failed (message printed), 3 usage error.
"""
import json
import os
import re
import shutil
import subprocess
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
LOCKFILES = (("pnpm-lock.yaml", ["pnpm", "install", "--lockfile-only"]),
             ("package-lock.json", ["npm", "install", "--package-lock-only"]),
             ("yarn.lock", ["yarn", "install", "--mode", "update-lockfile"]))


class GitError(Exception):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise GitError("git {}: {}".format(" ".join(args[:3]), r.stderr.decode("utf-8", "replace").strip()))
    return r


def changed(repo, path):
    """True if path differs from HEAD (modified, new or deleted)."""
    return bool(git(repo, "status", "--porcelain", "--", path).stdout.strip())


def read_state(run_dir):
    state = {}
    p = os.path.join(run_dir, "state.md")
    if os.path.isfile(p):
        for line in open(p):
            if ":" in line:
                k, v = line.split(":", 1)
                state.setdefault(k.strip(), v.strip())
    return state


def copy_code(run_dir, repo):
    src = os.path.join(run_dir, "code")
    for dirpath, _, names in os.walk(src):
        for n in names:
            if n == ".DS_Store":
                continue
            s = os.path.join(dirpath, n)
            d = os.path.join(repo, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)


def run_script(name, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name)] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in r.stdout.decode("utf-8", "replace").strip().splitlines():
        print("  " + line)


def update_lockfile(repo, package_jsons):
    """Update the lockfile if a package.json changed; return its path (or None)."""
    if not any(changed(repo, p) for p in package_jsons):
        return None
    for lock, cmd in LOCKFILES:
        if os.path.isfile(os.path.join(repo, lock)):
            r = subprocess.run(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if r.returncode != 0:
                print("commit-code: WARNING: `{}` failed; Verify's install step will flag the lockfile:".format(" ".join(cmd)))
                print("\n".join("  " + l for l in r.stdout.decode("utf-8", "replace").strip().splitlines()[-10:]))
                return None
            print("commit-code: lockfile updated ({})".format(lock))
            return lock
    return None


def commit(repo, files, subject, body_lines, run_id):
    files = [f for f in files if changed(repo, f)]
    if not files:
        return None
    git(repo, "add", "-A", "--", *files)
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    body = "\n".join(body_lines + ["", "Files:"] + ["- " + f for f in files] + ["", "Run: " + run_id]).strip()
    git(repo, "commit", "-m", subject, "-m", body)
    sha = git(repo, "rev-parse", "--short", "HEAD").stdout.decode().strip()
    print("commit-code: {} {} ({} file{})".format(sha, subject, len(files), "" if len(files) == 1 else "s"))
    return sha


def plan_groups(run_dir, contract, coder_files):
    """[(subject, [files])] for the first pass."""
    commits = contract.get("commits")
    if isinstance(commits, list) and commits:
        return [(c["message"].strip(), list(c["files"])) for c in commits], "contract.json commits"
    task = read_state(run_dir).get("task", "").strip() or "implement the run's changes"
    task = re.sub(r"\s*\(?(see|full text).*$", "", task, flags=re.I).strip() or "implement the run's changes"
    task = task[:60].rstrip(" .,;:")
    sp = os.path.join(run_dir, "coder-slices.json")
    if os.path.isfile(sp):
        slices = json.load(open(sp)).get("slices") or []
        if len(slices) > 1:
            groups = [("feat: {} ({})".format(task, s["label"]), [f["path"] for f in s["files"]]) for s in slices]
            return groups, "coder slices (contract.json has no commits plan)"
    return [("feat: " + task, list(coder_files))], "one commit (contract.json has no commits plan)"


def main():
    a = sys.argv[1:]
    if len(a) < 2 or (len(a) > 2 and a[2] not in ("--fix", "--lockfile")) or (len(a) > 2 and a[2] == "--fix" and len(a) != 4):
        print(__doc__)
        return 3
    repo, run_id = os.path.abspath(a[0]), a[1]
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    cpath = os.path.join(run_dir, "contract.json")
    if not os.path.isfile(cpath):
        print("ERROR: runs/{}/contract.json not found".format(run_id), file=sys.stderr)
        return 3
    contract = json.load(open(cpath))
    coder_files = [e["path"] for e in contract.get("files", []) if e.get("owner") == "coder"]
    package_jsons = [p for p in coder_files if p.endswith("package.json")] or ["package.json"]

    try:
        if len(a) > 2 and a[2] == "--lockfile":
            lock = update_lockfile(repo, package_jsons) or next((l for l, _ in LOCKFILES if changed(repo, l)), None)
            if not lock or not commit(repo, [lock], "chore: update lockfile", [], run_id):
                print("commit-code: lockfile already up to date, nothing committed")
            return 0

        copy_code(run_dir, repo)
        print("commit-code: copied runs/{}/code/ into the repo".format(run_id))
        run_script("autofix.py", repo, run_id, "code")
        run_script("regen.py", repo, run_id, "run")
        lock = update_lockfile(repo, package_jsons)

        made = []
        if len(a) > 2 and a[2] == "--fix":
            summary = a[3].strip()
            subject = summary if re.match(r"^\w+(\([^)]*\))?!?: ", summary) else "fix: " + summary
            sha = commit(repo, coder_files + ([lock] if lock else []), subject, [], run_id)
            made += [sha] if sha else []
        else:
            groups, source = plan_groups(run_dir, contract, coder_files)
            print("commit-code: grouping from {}".format(source))
            grouped = set()
            for subject, files in groups:
                if lock and any(f.endswith("package.json") for f in files):
                    files = files + [lock]
                grouped.update(files)
                sha = commit(repo, files, subject, [], run_id)
                made += [sha] if sha else []
            if lock and lock not in grouped:
                sha = commit(repo, [lock], "chore: update lockfile", [], run_id)
                made += [sha] if sha else []
            leftovers = [f for f in coder_files if f not in grouped]
            sha = commit(repo, leftovers, "chore: remaining changes of this run", [
                "These files changed but no planned commit listed them."], run_id)
            if sha:
                print("commit-code: WARNING: {} file(s) were in no planned commit; committed them together".format(
                    len([f for f in leftovers if f])))
                made.append(sha)

        others = git(repo, "status", "--porcelain").stdout.decode("utf-8", "replace").strip()
        if others:
            print("commit-code: WARNING: uncommitted changes outside this run's files (autofix/regen side effects?):")
            print("\n".join("  " + l for l in others.splitlines()[:20]))
        print("commit-code: {} commit(s) made".format(len(made)))
        return 0
    except GitError as e:
        print("commit-code: ERROR: {}".format(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
