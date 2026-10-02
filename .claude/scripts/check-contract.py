#!/usr/bin/env python3
"""
check-contract.py: mechanical Interface Contract gate, driven by runs/{RUN_ID}/contract.json.

Replaces check-contract.sh for runs that have a contract.json. The old script grepped
plan.md's prose for names and produced recurring false positives (pre-existing testids,
test-only fixture testids, template testids, removed testids, packages added in the same
run). Here every name has an explicit category, so each check is exact.

Usage:
  check-contract.py <run_id> <repo_path> validate   # after the Architect, before the Tester
  check-contract.py <run_id> <repo_path> tests      # after the Tester, before the reviewer
  check-contract.py <run_id> <repo_path> code       # after the Coder, before the sandbox
Options:
  --orch-root DIR   orchestrator project root (default: two levels above this script)

Exit: 0 clean, 1 violations (one per line, each starting with a check id), 3 setup error.

contract.json (written by the Architect):
{
  "version": 1,
  "files": [                                   # every file this run creates or modifies
    {"path": "apps/web/src/lib/x.ts",      "action": "create", "owner": "coder"},
    {"path": "apps/web/src/lib/x.test.ts", "action": "create", "owner": "tester"}
  ],
  "exports": [                                 # names other files/tests import
    {"file": "apps/web/src/lib/x.ts", "names": ["buildX", "X_HEADER"], "default": false}
  ],
  "testids": {
    "new":       [{"id": "x-root", "file": "apps/web/src/components/x.tsx"}],   # this run adds them
    "existing":  [{"id": "site-nav", "file": "apps/web/src/components/layout/site-nav.tsx"}],
    "templates": [{"template": "{prefix}-filter-country", "file": "...", "instances": ["catalog-filter-country"]}],
    "test_only": ["mock-chart"],               # only inside test fixtures / vi.mock factories
    "removed":   ["language-switcher-en"]      # removed by this run; tests may assert absence
  },
  "packages": {"added": ["cookie-parser"]}     # new dependencies this run adds to a package.json
}
"""

import json
import os
import re
import sys

BUILTINS = set("""assert assert/strict async_hooks buffer child_process cluster console constants crypto
dgram diagnostics_channel dns dns/promises domain events fs fs/promises http http2 https inspector
inspector/promises module net os path path/posix path/win32 perf_hooks process punycode querystring
readline readline/promises repl stream stream/promises string_decoder sys test timers timers/promises
tls trace_events tty url util util/types v8 vm wasi worker_threads zlib""".split())
EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json")
SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue")


# ── helpers ──────────────────────────────────────────────────────────────────

def load_jsonc(path):
    """JSON with comments and trailing commas (tsconfig style)."""
    text = open(path, encoding="utf-8").read()
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c); i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def files_under(root):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _, names in os.walk(root):
        for name in names:
            if name != ".DS_Store":
                out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(out)


def plan_files_table(plan_path):
    if not os.path.exists(plan_path):
        return None
    text = open(plan_path, encoding="utf-8").read()
    m = re.search(r"^##\s+Files changed\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return None
    files = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        path = cells[0].strip("`").strip() if cells else ""
        if not path or path.lower() in ("file", "path") or set(path) <= set("-: "):
            continue
        while path.startswith("./"):
            path = path[2:]
        files.append(path)
    return files


def strip_comments(src):
    """Remove // and /* */ comments (keeps strings intact well enough for import scanning)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:'\"`\\])//[^\n]*", r"\1", src)


# ── repo model: workspace members, deps, path aliases ───────────────────────

class Repo:
    def __init__(self, path):
        self.path = path
        self.members = self._members()                 # repo-relative dirs, "" = root
        self.deps = self._deps()
        self.aliases = self._aliases()                 # {member_dir: [(prefix, [targets], wildcard)]}

    def exists(self, rel):
        return os.path.isfile(os.path.join(self.path, rel))

    def _members(self):
        members = {""}
        patterns = []
        ws = os.path.join(self.path, "pnpm-workspace.yaml")
        if os.path.exists(ws):
            inside = False
            for line in open(ws, encoding="utf-8"):
                if re.match(r"^packages:", line):
                    inside = True; continue
                if inside:
                    m = re.match(r"^\s*-\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
                    if m:
                        patterns.append(m.group(1).strip()); continue
                    if re.match(r"^\S", line):
                        inside = False
        pkg = os.path.join(self.path, "package.json")
        if os.path.exists(pkg):
            try:
                w = json.load(open(pkg)).get("workspaces")
                if isinstance(w, dict):
                    w = w.get("packages")
                patterns += list(w or [])
            except ValueError:
                pass
        for p in patterns:
            p = p.rstrip("/")
            if p.startswith("!"):
                continue
            if p.endswith("/*") or p.endswith("/**"):
                base = p.split("/*")[0]
                d = os.path.join(self.path, base)
                if os.path.isdir(d):
                    for e in sorted(os.listdir(d)):
                        if os.path.isdir(os.path.join(d, e)):
                            members.add(base + "/" + e)
            else:
                members.add(p)
        return sorted(members, key=len, reverse=True)

    def _deps(self):
        deps = set()
        for m in self.members:
            pj = os.path.join(self.path, m, "package.json")
            if os.path.exists(pj):
                try:
                    d = json.load(open(pj))
                except ValueError:
                    continue
                if d.get("name"):
                    deps.add(d["name"])
                for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    deps.update((d.get(k) or {}).keys())
        return deps

    def _tsconfig_paths(self, tsconfig, depth=0):
        """(baseUrl dir, paths) following `extends` (relative) a few levels."""
        try:
            cfg = load_jsonc(tsconfig)
        except (ValueError, OSError):
            return None, {}
        here = os.path.dirname(tsconfig)
        co = cfg.get("compilerOptions") or {}
        base_dir, paths = None, {}
        ext = cfg.get("extends")
        if isinstance(ext, str) and ext.startswith(".") and depth < 4:
            parent = os.path.normpath(os.path.join(here, ext))
            if not parent.endswith(".json"):
                parent += ".json"
            if os.path.exists(parent):
                base_dir, paths = self._tsconfig_paths(parent, depth + 1)
        if "baseUrl" in co:
            base_dir = os.path.normpath(os.path.join(here, co["baseUrl"]))
        if co.get("paths"):
            paths = co["paths"]
            if base_dir is None:
                base_dir = here
        return base_dir, paths

    def _aliases(self):
        table = {}
        for m in self.members:
            ts = os.path.join(self.path, m, "tsconfig.json")
            if not os.path.exists(ts):
                continue
            base_dir, paths = self._tsconfig_paths(ts)
            entries = []
            for key, targets in (paths or {}).items():
                wildcard = key.endswith("*")
                prefix = key[:-1] if wildcard else key
                tgt = []
                for t in targets or []:
                    absolute = os.path.normpath(os.path.join(base_dir or os.path.join(self.path, m), t.rstrip("*")))
                    rel = os.path.relpath(absolute, self.path).replace(os.sep, "/")
                    tgt.append(rel + ("/" if t.endswith("/*") and not rel.endswith("/") else ""))
                entries.append((prefix, tgt, wildcard))
            entries.sort(key=lambda e: len(e[0]), reverse=True)
            table[m] = entries
        return table

    def member_of(self, rel):
        for m in self.members:
            if m == "" or rel == m or rel.startswith(m + "/"):
                return m
        return ""

    def alias_targets(self, spec, from_rel):
        """Repo-relative base paths an aliased specifier maps to, or None if it isn't an alias."""
        member = self.member_of(from_rel)
        for m in [member, ""]:
            for prefix, targets, wildcard in self.aliases.get(m, []):
                if (wildcard and spec.startswith(prefix)) or (not wildcard and spec == prefix):
                    rest = spec[len(prefix):] if wildcard else ""
                    return [os.path.normpath(t + rest).replace(os.sep, "/") if rest else t.rstrip("/") for t in targets]
        return None


def candidates(base):
    """Files a module specifier base path could resolve to."""
    out = [base] + [base + e for e in EXTS] + [base + "/index" + e for e in EXTS]
    m = re.match(r"^(.*)\.(m?js|cjs|jsx)$", base)
    if m:  # NodeNext-style `./x.js` that is really x.ts
        out += [m.group(1) + e for e in (".ts", ".tsx", ".mts", ".cts")]
    return out


# ── contract ─────────────────────────────────────────────────────────────────

class Contract:
    def __init__(self, data):
        self.data = data
        self.files = data.get("files") or []
        self.paths = [f.get("path", "") for f in self.files]
        t = data.get("testids") or {}
        self.new = t.get("new") or []
        self.existing = t.get("existing") or []
        self.templates = t.get("templates") or []
        self.test_only = set(t.get("test_only") or [])
        self.removed = set(t.get("removed") or [])
        self.exports = data.get("exports") or []
        self.added_packages = set((data.get("packages") or {}).get("added") or [])

    def owner(self, path):
        for f in self.files:
            if f.get("path") == path:
                return f.get("owner")
        return None

    def allowed_testids(self):
        ids = {x.get("id") for x in self.new} | {x.get("id") for x in self.existing}
        for t in self.templates:
            ids |= set(t.get("instances") or [])
        return ids | self.test_only | self.removed


def schema_errors(data):
    errs = []
    if not isinstance(data, dict):
        return ["SCHEMA: contract.json must be a JSON object"]
    if not isinstance(data.get("files"), list) or not data["files"]:
        errs.append("SCHEMA: `files` must be a non-empty list")
    seen = set()
    for i, f in enumerate(data.get("files") or []):
        p = f.get("path") if isinstance(f, dict) else None
        if not p or not isinstance(p, str):
            errs.append("SCHEMA: files[{}] needs a string `path`".format(i)); continue
        if p.startswith(("/", "./")) or ".." in p.split("/"):
            errs.append("SCHEMA: files[{}].path must be repo-relative without ./ or ..: {}".format(i, p))
        if p in seen:
            errs.append("SCHEMA: duplicate file {}".format(p))
        seen.add(p)
        if f.get("action") not in ("create", "modify"):
            errs.append("SCHEMA: {}: `action` must be \"create\" or \"modify\"".format(p))
        if f.get("owner") not in ("coder", "tester"):
            errs.append("SCHEMA: {}: `owner` must be \"coder\" or \"tester\"".format(p))
    t = data.get("testids") or {}
    if not isinstance(t, dict):
        errs.append("SCHEMA: `testids` must be an object")
        t = {}
    for key in ("new", "existing"):
        for x in t.get(key) or []:
            if not isinstance(x, dict) or not x.get("id") or not x.get("file"):
                errs.append("SCHEMA: testids.{} entries need `id` and `file`: {}".format(key, json.dumps(x)))
    for x in t.get("templates") or []:
        if not isinstance(x, dict) or not x.get("template") or not x.get("file"):
            errs.append("SCHEMA: testids.templates entries need `template` and `file`: {}".format(json.dumps(x)))
    ids_new = [x.get("id") for x in t.get("new") or [] if isinstance(x, dict)]
    ids_old = [x.get("id") for x in t.get("existing") or [] if isinstance(x, dict)]
    for dup in sorted({i for i in ids_new if ids_new.count(i) > 1}):
        errs.append("SCHEMA: testid {} is declared twice in testids.new".format(dup))
    for both in sorted(set(ids_new) & set(ids_old)):
        errs.append("SCHEMA: testid {} is in both testids.new and testids.existing".format(both))
    for e in data.get("exports") or []:
        if not isinstance(e, dict) or not e.get("file"):
            errs.append("SCHEMA: exports entries need `file`: {}".format(json.dumps(e)))
    return errs


# ── checks ───────────────────────────────────────────────────────────────────

def check_validate(c, repo, plan_path):
    v = []
    table = plan_files_table(plan_path)
    if table is None:
        v.append("PLAN_TABLE_MISSING: plan.md has no `## Files changed` table")
    else:
        for p in sorted(set(table) - set(c.paths)):
            v.append("CONTRACT_DRIFT: {} is in plan.md's Files changed table but not in contract.json files".format(p))
        for p in sorted(set(c.paths) - set(table)):
            v.append("CONTRACT_DRIFT: {} is in contract.json files but not in plan.md's Files changed table".format(p))
    for f in c.files:
        p = f.get("path")
        if f.get("action") == "create" and repo.exists(p):
            v.append("CREATE_FILE_EXISTS: {} is marked create but already exists in the repo (use modify)".format(p))
        if f.get("action") == "modify" and not repo.exists(p):
            v.append("MODIFY_FILE_MISSING: {} is marked modify but doesn't exist in the repo".format(p))
    for x in c.existing:
        if not repo.exists(x["file"]):
            v.append("EXISTING_TESTID_FILE_MISSING: testid {} is declared existing in {}, which doesn't exist".format(x["id"], x["file"]))
        elif not re.search(r"""['"`]""" + re.escape(x["id"]) + r"""['"`]""", open(os.path.join(repo.path, x["file"]), encoding="utf-8", errors="replace").read()):
            v.append("EXISTING_TESTID_NOT_FOUND: testid {} is declared existing but {} doesn't contain it".format(x["id"], x["file"]))
    for x in c.new:
        if c.owner(x["file"]) != "coder":
            v.append("NEW_TESTID_FILE_NOT_IN_CONTRACT: testid {} is new in {}, which isn't a coder-owned file in contract.json".format(x["id"], x["file"]))
    for t in c.templates:
        if c.owner(t["file"]) != "coder" and not repo.exists(t["file"]):
            v.append("TEMPLATE_FILE_UNKNOWN: template {} names {}, which is neither in the contract nor in the repo".format(t["template"], t["file"]))
    for e in c.exports:
        if e["file"] not in c.paths and not repo.exists(e["file"]):
            v.append("EXPORT_FILE_UNKNOWN: exports for {}, which is neither in the contract nor in the repo".format(e["file"]))
    for pkg in sorted(c.added_packages & repo.deps):
        v.append("PACKAGE_ALREADY_PRESENT: {} is listed in packages.added but is already a dependency".format(pkg))
    if c.added_packages and not any(p.endswith("package.json") for p in c.paths):
        v.append("PACKAGE_JSON_NOT_IN_CONTRACT: packages.added is non-empty but no package.json is in contract.json files")
    return v


TESTID_QUERY = re.compile(r"(?:get|query|find)(?:All)?ByTestId\(\s*(['\"`])((?:(?!\1).)+)\1")
TESTID_SELECTOR = re.compile(r"""data-testid\s*=\s*(?:\\?["']|\{\s*['"`])([^"'`}\\]+)""")
IMPORT_RE = re.compile(
    r"""(?:^|[;\s])(?:import|export)\s+(?:type\s+)?(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]"""
    r"""|\brequire\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|\bimport\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|\b(?:vi|jest)\.(?:mock|doMock|unmock|importActual|requireActual|importMock)\(\s*['"]([^'"]+)['"]""",
    re.M)


def test_files(tests_dir):
    return [f for f in files_under(tests_dir) if f.endswith(SOURCE_EXTS)]


def check_tests(c, repo, tests_dir):
    v = []
    allowed = c.allowed_testids()
    produced = set(files_under(tests_dir))
    for rel in test_files(tests_dir):
        src_raw = open(os.path.join(tests_dir, rel), encoding="utf-8", errors="replace").read()
        src = strip_comments(src_raw)

        # 1. testids
        used = {m.group(2) for m in TESTID_QUERY.finditer(src)} | set(TESTID_SELECTOR.findall(src))
        for tid in sorted(used):
            if "${" in tid:
                continue  # built at runtime; can't be checked statically
            if tid not in allowed:
                v.append("TESTID_NOT_IN_CONTRACT: {} uses data-testid \"{}\", which contract.json doesn't declare "
                         "(new, existing, template instance, test_only or removed)".format(rel, tid))

        # 2 + 3. imports
        for m in IMPORT_RE.finditer(src):
            spec = next(g for g in m.groups() if g)
            if spec.startswith(("http:", "https:", "data:", "virtual:")):
                continue
            if spec.startswith("."):
                base = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
                bases = [base]
            else:
                bases = repo.alias_targets(spec, rel)
                if bases is None and spec.startswith(("@/", "~/")):
                    # No tsconfig paths found: assume the common default (member's src/).
                    member = repo.member_of(rel)
                    bases = [((member + "/") if member else "") + "src/" + spec[2:]]
                if bases is None:
                    bare = spec[5:] if spec.startswith("node:") else spec
                    if bare in BUILTINS or bare.split("/")[0] in BUILTINS:
                        continue
                    pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                    types_pkg = "@types/" + (pkg[1:].replace("/", "__") if pkg.startswith("@") else pkg)
                    # A package with only @types/ declared is fine for type-position imports
                    # (`import type`, `import('x').T`); runtime resolution is checked by Verify.
                    if pkg not in repo.deps and types_pkg not in repo.deps and pkg not in c.added_packages:
                        v.append("UNKNOWN_PACKAGE: {} imports '{}' (package {}), which no package.json in the repo "
                                 "depends on and contract.json's packages.added doesn't list".format(rel, spec, pkg))
                    continue
            ok = False
            for b in bases:
                for cand in candidates(b):
                    if repo.exists(cand) or cand in c.paths or cand in produced:
                        ok = True; break
                if ok:
                    break
            if not ok:
                v.append("IMPORT_NOT_RESOLVED: {} imports '{}', which resolves to neither an existing repo file "
                         "nor a file in contract.json (tried {})".format(rel, spec, ", ".join(bases)))

        # 4. banned patterns
        if re.search(r"while\s*\(\s*true\s*\)", src):
            v.append("BANNED_PATTERN: {} contains while(true)".format(rel))
        calls_network = re.search(r"(?<![\w.])fetch\(|\baxios[.(]", src)
        mocked = re.search(r"\b(?:vi|jest)\.(?:mock|doMock)\(|\bvi\.stubGlobal\(\s*['\"]fetch|"
                           r"\b(?:vi|jest)\.spyOn\(\s*(?:global|globalThis|window)\s*,\s*['\"]fetch|"
                           r"\bsetupServer\(|\bnock\(|(?:global|globalThis|window)\.fetch\s*=", src)
        if calls_network and not mocked:
            v.append("BANNED_PATTERN: {} calls fetch()/axios without mocking it (vi.mock, vi.stubGlobal('fetch'), "
                     "vi.spyOn(globalThis, 'fetch'), msw or nock)".format(rel))
    return v


EXPORT_PATTERNS = (
    r"export\s+(?:declare\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|var|class|interface|type|enum|abstract\s+class)\s+{name}\b",
    r"export\s*\{{[^}}]*\b{name}\b[^}}]*\}}",
    r"export\s+\*\s+as\s+{name}\b",
)


def has_export(src, name):
    return any(re.search(p.format(name=re.escape(name)), src) for p in EXPORT_PATTERNS)


def check_code(c, repo, run_dir):
    v = []
    code_dir, tests_dir = os.path.join(run_dir, "code"), os.path.join(run_dir, "tests")

    def code_src(path):
        p = os.path.join(code_dir, path)
        return open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else None

    def literal(tid):
        return re.compile(r"""['"`]""" + re.escape(tid) + r"""['"`]""")

    for x in c.new:
        src = code_src(x["file"])
        if src is None:
            v.append("MISSING_TESTID_IN_CODE: testid {} should be in {}, but the Coder didn't produce that file".format(x["id"], x["file"]))
        elif not literal(x["id"]).search(src):
            v.append("MISSING_TESTID_IN_CODE: testid {} isn't in {} (expected the literal string)".format(x["id"], x["file"]))
    for t in c.templates:
        src = code_src(t["file"])
        if src is None:
            continue  # template lives in an unchanged file
        static = [s for s in re.split(r"\{[^}]*\}", t["template"]) if s]
        if not all(s in src for s in static):
            v.append("MISSING_TESTID_IN_CODE: template {} not found in {} (looked for {})".format(t["template"], t["file"], static))
    for x in c.existing:
        src = code_src(x["file"])
        if src is not None and not literal(x["id"]).search(src):
            v.append("EXISTING_TESTID_REMOVED: {} rewrites {} and drops existing testid {}, which tests rely on".format("the Coder", x["file"], x["id"]))
    for e in c.exports:
        if c.owner(e["file"]) != "coder":
            continue
        src = code_src(e["file"])
        if src is None:
            v.append("MISSING_EXPORT: {} declares exports but the Coder didn't produce the file".format(e["file"]))
            continue
        if e.get("default") and not re.search(r"export\s+default\b|export\s*\{[^}]*\bas\s+default\b", src):
            v.append("MISSING_EXPORT: {} must have a default export".format(e["file"]))
        for name in e.get("names") or []:
            if not has_export(src, name):
                v.append("MISSING_EXPORT: {} doesn't export {}".format(e["file"], name))

    sentinel = os.path.join(run_dir, ".test_reviewer_passed_at")
    if os.path.exists(sentinel):
        stamp = os.path.getmtime(sentinel)
        for rel in files_under(tests_dir):
            if os.path.getmtime(os.path.join(tests_dir, rel)) > stamp:
                v.append("TESTS_MODIFIED_AFTER_REVIEW: tests/{} changed after the test-reviewer approved".format(rel))
    return v


def main():
    argv = sys.argv[1:]
    orch_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if "--orch-root" in argv:
        i = argv.index("--orch-root")
        orch_root = os.path.abspath(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) != 3 or argv[2] not in ("validate", "tests", "code"):
        print(__doc__)
        sys.exit(3)
    run_id, repo_path, mode = argv
    run_dir = os.path.join(orch_root, "runs", run_id)
    contract_path = os.path.join(run_dir, "contract.json")
    if not os.path.exists(contract_path):
        print("SETUP_ERROR: {} not found (old runs without it use check-contract.sh)".format(contract_path))
        sys.exit(3)
    try:
        data = json.load(open(contract_path, encoding="utf-8"))
    except ValueError as e:
        print("SCHEMA: contract.json is not valid JSON: {}".format(e))
        sys.exit(1)
    errs = schema_errors(data)
    if errs:
        print("\n".join(errs))
        sys.exit(1)
    c, repo = Contract(data), Repo(os.path.abspath(repo_path))

    if mode == "validate":
        v = check_validate(c, repo, os.path.join(run_dir, "plan.md"))
    elif mode == "tests":
        if not os.path.isdir(os.path.join(run_dir, "tests")):
            print("SETUP_ERROR: runs/{}/tests/ not found".format(run_id)); sys.exit(3)
        v = check_tests(c, repo, os.path.join(run_dir, "tests"))
    else:
        v = check_code(c, repo, run_dir)

    if v:
        print("\n".join(v))
        sys.exit(1)
    print("check-contract {}: clean ({})".format(mode, run_id))


if __name__ == "__main__":
    main()
