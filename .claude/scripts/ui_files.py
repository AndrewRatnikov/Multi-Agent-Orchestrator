"""
ui_files.py: which of a run's files are UI, shared by check-contract.py and render-config.py.

A run that changes UI must either declare render targets or say why nothing visible changes
(`render_targets_none_reason`). This module decides what "changes UI" means, so both scripts
agree on it.

UI file = a coder-owned file in contract.json that matches a UI pattern and is not a test file.

Default patterns: .tsx, .jsx, .vue, .svelte and stylesheets (.css, .scss, .sass, .less, .styl,
.pcss). A repo can replace them with a `ui_paths` list in the `render` or the `sizing` section
of its .claude/verify.json (`render` wins when both have one), read from the run's
original_branch, like the rest of the per-repo config, so a run can't loosen its own check:

  "render": { "ui_paths": ["apps/web/src/**/*.tsx", "apps/web/src/**/*.css"] }

Patterns: a pattern without "/" matches a file name anywhere; `**/` at the start also matches
at the root (the same rules as size-run.py's sensitive_paths). An empty list means the repo
has no UI files at all.

Test files never count: *.test.*, *.spec.*, and anything under __tests__/ or __mocks__/.
"""

import fnmatch
import json
import os
import re
import subprocess

DEFAULT_UI_PATTERNS = ["*.tsx", "*.jsx", "*.vue", "*.svelte",
                       "*.css", "*.scss", "*.sass", "*.less", "*.styl", "*.pcss"]
TEST_FILE = re.compile(r"(^|/)(__tests__|__mocks__)/|\.(test|spec)\.[^/]+$")


def matches(path, pattern):
    if "/" not in pattern:
        return fnmatch.fnmatch(os.path.basename(path), pattern)
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def read_state(run_dir):
    state = {}
    p = os.path.join(run_dir, "state.md")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if ":" in line:
                k, v = line.split(":", 1)
                state[k.strip()] = v.strip()
    return state


def repo_ui_patterns(repo, ref):
    """(patterns, note): the repo's ui_paths from `ref`:.claude/verify.json, or the defaults."""
    if not ref:
        return DEFAULT_UI_PATTERNS, "default UI patterns (no original_branch)"
    r = subprocess.run(["git", "-C", repo, "show", "{}:.claude/verify.json".format(ref)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return DEFAULT_UI_PATTERNS, "default UI patterns"
    try:
        cfg = json.loads(r.stdout.decode("utf-8"))
    except ValueError:
        return DEFAULT_UI_PATTERNS, "default UI patterns ({}:.claude/verify.json is not valid JSON)".format(ref)
    for section in ("render", "sizing"):
        sec = cfg.get(section) if isinstance(cfg, dict) else None
        if isinstance(sec, dict) and "ui_paths" in sec:
            pats = sec["ui_paths"]
            if isinstance(pats, list) and all(isinstance(p, str) and p for p in pats):
                return pats, "ui_paths from {}.ui_paths on {}".format(section, ref)
            return DEFAULT_UI_PATTERNS, "default UI patterns ({}.ui_paths on {} is not a list of strings)".format(section, ref)
    return DEFAULT_UI_PATTERNS, "default UI patterns"


def ui_files(contract, repo, run_dir):
    """(files, note): coder-owned, non-test UI files in contract.json's `files`."""
    ref = read_state(run_dir).get("original_branch") if run_dir else None
    patterns, note = repo_ui_patterns(repo, ref)
    out = []
    for f in (contract.get("files") or []) if isinstance(contract, dict) else []:
        p = f.get("path", "") if isinstance(f, dict) else ""
        if f.get("owner") == "coder" and p and not TEST_FILE.search(p) and any(matches(p, x) for x in patterns):
            out.append(p)
    return out, note


def short_list(files, n=4):
    return ", ".join(files[:n]) + (", ... ({} in all)".format(len(files)) if len(files) > n else "")
