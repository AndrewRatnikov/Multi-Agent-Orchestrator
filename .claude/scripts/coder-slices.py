#!/usr/bin/env python3
"""coder-slices.py: split a big first Coder pass into sequential slices, each with a fresh context.

One Coder holding every file of a large run re-sends all of it on every call (28 Oct:
43 files, ~376k tokens of context per call, 56% of the run). Run in slices, each pass
reads only its own files and the tests that exercise them; files from earlier slices
are already done in runs/<run>/code/.

Usage:
  coder-slices.py plan <run_id> <repo>
      Writes runs/<run_id>/coder-slices.json and prints one line per slice.
      Exit 0: sliced (N > 1, run the Coder once per slice, in order)
      Exit 1: small enough for a single pass (the file still lists one slice)
      Exit 3: setup error
  coder-slices.py set <run_id> <k>
      Marks slice k as the one in progress (runs/<run_id>/.coder-slice), so the write
      guard blocks files of LATER slices. k = 0 means no slicing (retries, single pass).

Slicing (mechanical, from contract.json's Coder-owned files; generated files from
generated.json are left out, regen.py rebuilds them):
  - single pass when there are at most 12 files and at most 250 KB of MODIFY files;
  - otherwise by layer, in dependency order: 1. data & core (lib, server, db, config),
    2. app & UI (components, routes, pages, hooks), 3. content & docs (i18n, content,
    markdown). A layer over 2x the budget (24 files or 500 KB) is cut in two; a slice of
    fewer than 3 files and under 40 KB is merged into its neighbour.
  - Within a slice: type files first, then new files, then modified ones, by path.
  - Each slice lists the test files whose imports resolve to one of its files.
"""
import json
import math
import os
import posixpath
import re
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAX_FILES, MAX_KB = 12, 250
CREATE_KB_GUESS = 6
LABELS = ["data & core", "app & UI", "content & docs"]
CONTENT = re.compile(r"\.mdx?$|(^|/)(i18n|locales?|content|messages|translations)/")
CORE = re.compile(r"(^|/)(lib|server|db|database|api|supabase|prisma|migrations|packages|utils|types|store|services|models)/"
                  r"|(^|/)package\.json$|\.sql$|\.config\.[cm]?[jt]s$")
UI = re.compile(r"(^|/)(components|routes|pages|app|hooks|views|screens|layouts|ui)/|\.(tsx|jsx|vue|svelte)$")
IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)['"]([^'"]+)['"]""")
STRIP_EXT = re.compile(r"\.(d\.)?[cm]?[jt]sx?$|\.vue$|\.svelte$")


def layer(path):
    if CONTENT.search(path):
        return 2
    if CORE.search(path):
        return 0
    if UI.search(path):
        return 1
    return 0


def order_key(entry):
    p = entry["path"]
    is_types = bool(re.search(r"(^|/)types?(\.d)?\.[cm]?[jt]sx?$|(^|/)types/", p))
    return (0 if is_types else 1, 0 if entry["action"] == "create" else 1, p)


def size_kb(run_dir, repo, entry):
    if entry["action"] == "create":
        return CREATE_KB_GUESS
    for base in (os.path.join(run_dir, "code"), repo):
        f = os.path.join(base, entry["path"])
        if os.path.isfile(f):
            return max(1, os.path.getsize(f) // 1024)
    return CREATE_KB_GUESS


def cut(entries, parts):
    """Cut an ordered list into `parts` contiguous chunks of similar weight."""
    total = sum(e["kb"] + 2 for e in entries)
    out, cur, acc, target = [], [], 0, total / parts
    for e in entries:
        cur.append(e)
        acc += e["kb"] + 2
        if acc >= target * (len(out) + 1) and len(out) < parts - 1:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def test_imports(run_dir):
    """test path -> set of repo-relative module paths (no extension) it imports."""
    root = os.path.join(run_dir, "tests")
    found = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                text = open(full, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            mods = set()
            for spec in IMPORT.findall(text):
                if spec.startswith("."):
                    mods.add(("abs", STRIP_EXT.sub("", posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec)))))
                elif re.match(r"^[@~#]/", spec) or spec.startswith("src/"):
                    mods.add(("tail", STRIP_EXT.sub("", re.sub(r"^[@~#]/", "", spec))))
            found[rel] = mods
    return found


def matches(module_path, mods):
    m = STRIP_EXT.sub("", module_path)
    m_index = re.sub(r"/index$", "", m)
    for kind, spec in mods:
        if kind == "abs" and (spec == m or spec == m_index):
            return True
        if kind == "tail" and (m == spec or m.endswith("/" + spec) or m_index.endswith("/" + spec)):
            return True
    return False


def plan(run_id, repo):
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    contract = os.path.join(run_dir, "contract.json")
    if not os.path.isfile(contract):
        print("ERROR: runs/{}/contract.json not found".format(run_id), file=sys.stderr)
        return 3
    generated = set()
    gpath = os.path.join(run_dir, "generated.json")
    if os.path.isfile(gpath):
        generated = {e["path"] for e in json.load(open(gpath)).get("files") or []}
    entries = []
    for e in json.load(open(contract)).get("files", []):
        if isinstance(e, dict) and e.get("owner") == "coder" and e.get("path") and e["path"] not in generated:
            entry = {"path": e["path"], "action": e.get("action", "modify")}
            entry["kb"] = size_kb(run_dir, repo, entry)
            entries.append(entry)

    modify_kb = sum(e["kb"] for e in entries if e["action"] == "modify")
    if len(entries) <= MAX_FILES and modify_kb <= MAX_KB:
        slices = [sorted(entries, key=order_key)]
        labels = ["all files"]
        reason = "single pass: {} files, {} KB of modified files (limits {} files, {} KB)".format(
            len(entries), modify_kb, MAX_FILES, MAX_KB)
    else:
        slices, labels = [], []
        for i in range(3):
            group = sorted([e for e in entries if layer(e["path"]) == i], key=order_key)
            if not group:
                continue
            kb = sum(e["kb"] for e in group)
            parts = max(1, min(3, math.ceil(max(len(group) / (2 * MAX_FILES), kb / (2 * MAX_KB)))))
            chunks = cut(group, parts) if parts > 1 else [group]
            for j, chunk in enumerate(chunks):
                slices.append(chunk)
                labels.append(LABELS[i] + (" ({}/{})".format(j + 1, len(chunks)) if len(chunks) > 1 else ""))
        i = 0
        while len(slices) > 1 and i < len(slices):
            if len(slices[i]) < 3 and sum(e["kb"] for e in slices[i]) < 40:
                j = i - 1 if i > 0 else i + 1
                lo, hi = min(i, j), max(i, j)
                slices[lo:hi + 1] = [slices[lo] + slices[hi]]
                labels[lo:hi + 1] = [labels[lo] + " + " + labels[hi]]
                i = 0
            else:
                i += 1
        reason = "{} slices: {} files, {} KB of modified files (over {} files or {} KB)".format(
            len(slices), len(entries), modify_kb, MAX_FILES, MAX_KB)

    imports = test_imports(run_dir)
    out = []
    for k, (chunk, label) in enumerate(zip(slices, labels), 1):
        tests = sorted(t for t, mods in imports.items() if any(matches(e["path"], mods) for e in chunk))
        out.append({"index": k, "label": label, "kb": sum(e["kb"] for e in chunk),
                    "files": [{"path": e["path"], "action": e["action"]} for e in chunk], "tests": tests})
    unmatched = sorted(t for t in imports if not any(t in s["tests"] for s in out))
    data = {"run_id": run_id, "reason": reason, "slices": out,
            "tests_not_matched": unmatched, "generated_left_out": sorted(generated)}
    with open(os.path.join(run_dir, "coder-slices.json"), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print("coder-slices: " + reason)
    for s in out:
        print("  slice {}: {} — {} files, ~{} KB, {} test files".format(
            s["index"], s["label"], len(s["files"]), s["kb"], len(s["tests"])))
    if unmatched:
        print("  {} test file(s) match no Coder file by import; every slice may consult them".format(len(unmatched)))
    return 0 if len(out) > 1 else 1


def set_current(run_id, k):
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    if not os.path.isdir(run_dir) or not re.fullmatch(r"\d+", k):
        print("ERROR: usage: coder-slices.py set <run_id> <k>", file=sys.stderr)
        return 3
    with open(os.path.join(run_dir, ".coder-slice"), "w") as f:
        f.write(k + "\n")
    print("coder-slices: current slice = {}".format(k if k != "0" else "none (single pass)"))
    return 0


def main():
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "plan":
        return plan(a[1], os.path.abspath(a[2]))
    if len(a) == 3 and a[0] == "set":
        return set_current(a[1], a[2])
    print(__doc__)
    return 3


if __name__ == "__main__":
    sys.exit(main())
