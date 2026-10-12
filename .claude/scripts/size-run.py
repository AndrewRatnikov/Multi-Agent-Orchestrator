#!/usr/bin/env python3
"""
size-run.py: decide, mechanically, whether a run is `small` or `standard`.

Runs right after the Architect (Stage 2), when contract.json says exactly what the
run will touch. A small run keeps every mechanical gate (contract checks, sandbox,
Verify, render review) and only trims the LLM stages: no LLM Test-Reviewer, and
Sonnet instead of Opus for the Tester and Coder. Any trouble later escalates it
back to standard (run-orchestration.md, "Run size").

Usage:
  size-run.py <run_id> <repo>

A run is small only when ALL of these hold:
  - at most `max_files` coder-owned files in contract.json (tests don't count)   default 3
  - contract.json adds no package
  - no coder-owned file matches a sensitive path (the repo's list, or defaults)
  - no runs/{RUN_ID}/answers-*.md (the user made no product/design decision)
  - no `## Decisions` entry in plan.md is marked `ADR: yes`

Per-repo settings come from the `sizing` section of the target repo's
.claude/verify.json, read from the run's original branch (so a run can't loosen
its own sizing):

  "sizing": {
    "max_files": 3,
    "sensitive_paths": ["apps/api/prisma/**", "apps/api/src/auth/**", "src/lib/localDb.ts"]
  }

`sensitive_paths` is added to the built-in defaults, not a replacement. A pattern
without "/" matches a file name anywhere; `**/` at the start also matches at the root.

`size_override: small|full` in state.md (from `--size` on /run-orchestration) wins
over the rules; the reasons are still printed so the report shows what was overridden.

Output: `size: small|standard`, then one `- reason` per line.
Exit: 0 small, 1 standard, 3 usage/setup error.
"""

import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_MAX_FILES = 3
DEFAULT_SENSITIVE = [
    # schema, data migrations, raw SQL
    "**/prisma/**", "**/migrations/**", "*.sql",
    # auth, sessions, crypto, security
    "**/auth/**", "*auth*", "*session*", "*crypto*", "*security*", "*guard*",
    # dependencies
    "package.json", "pnpm-lock.yaml", "package-lock.json", "yarn.lock",
    # build, deploy and CI configuration
    ".github/**", "*.env*", "vite.config.*", "next.config.*", "wrangler.*",
    "render.yaml", "vercel.json", "tsconfig*.json", "Dockerfile*",
]


def die(msg):
    print(f"usage error: {msg}")
    sys.exit(3)


def matches(path, pattern):
    if "/" not in pattern:
        return fnmatch.fnmatch(os.path.basename(path), pattern)
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def state_field(text, name):
    m = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def repo_sizing(repo, original_branch):
    if not original_branch:
        return {}, "no original_branch in state.md; built-in defaults"
    r = subprocess.run(["git", "-C", repo, "show", f"{original_branch}:.claude/verify.json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {}, f"no .claude/verify.json on {original_branch}; built-in defaults"
    try:
        cfg = json.loads(r.stdout).get("sizing") or {}
    except json.JSONDecodeError as e:
        return {}, f".claude/verify.json on {original_branch} is not valid JSON ({e}); built-in defaults"
    if not cfg:
        return {}, f".claude/verify.json on {original_branch} has no `sizing` section; built-in defaults"
    return cfg, f"sizing config from .claude/verify.json on {original_branch}"


def main(argv):
    if len(argv) != 3:
        die("expected `<run_id> <repo>`")
    run_id, repo = argv[1], os.path.abspath(argv[2])
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    contract_path = os.path.join(run_dir, "contract.json")
    state_path = os.path.join(run_dir, "state.md")
    for p in (contract_path, state_path):
        if not os.path.isfile(p):
            die(f"{p} not found")
    try:
        contract = json.load(open(contract_path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"contract.json is not valid JSON: {e}")
    state = open(state_path, encoding="utf-8").read()

    cfg, cfg_note = repo_sizing(repo, state_field(state, "original_branch"))
    max_files = int(cfg.get("max_files", DEFAULT_MAX_FILES))
    sensitive = DEFAULT_SENSITIVE + list(cfg.get("sensitive_paths", []))

    reasons = []
    coder_files = [f["path"] for f in contract.get("files", []) if f.get("owner") == "coder"]
    if len(coder_files) > max_files:
        reasons.append(f"{len(coder_files)} code files (small allows {max_files})")
    added = (contract.get("packages") or {}).get("added") or []
    if added:
        reasons.append("adds packages: " + ", ".join(map(str, added)))
    for path in coder_files:
        hit = next((p for p in sensitive if matches(path, p)), None)
        if hit:
            reasons.append(f"sensitive path {path} (matches `{hit}`)")
    answers = sorted(glob.glob(os.path.join(run_dir, "answers-*.md")))
    if answers:
        reasons.append("the user answered questions (" + ", ".join(os.path.basename(a) for a in answers) + ")")
    plan_path = os.path.join(run_dir, "plan.md")
    if os.path.isfile(plan_path):
        m = re.search(r"^## Decisions[ \t]*\n(.*?)(?=^## |\Z)", open(plan_path, encoding="utf-8").read(), re.M | re.S)
        if m:
            for e in re.split(r"^### ", m.group(1).strip(), flags=re.M)[1:]:
                adr = re.search(r"^- \*\*ADR:\*\*[ \t]*(.*)$", e, re.M)
                if adr and adr.group(1).strip().lower().startswith("yes"):
                    reasons.append(f"plan.md decision {e.split(':')[0].strip()} is marked for an ADR")

    size = "standard" if reasons else "small"
    override = state_field(state, "size_override").lower()
    if override in ("small", "full", "standard"):
        forced = "small" if override == "small" else "standard"
        if forced != size:
            reasons.insert(0, f"overridden by --size {override} (rules said {size})")
        size = forced

    print(f"size: {size}")
    print(f"- {len(coder_files)} code file(s); {cfg_note}")
    for r in reasons:
        print(f"- {r}")
    return 0 if size == "small" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
